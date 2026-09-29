"""Detached, durable official-CLI execution. No network listener or agent engine."""
from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import math
import os
import signal
import time
import uuid
from pathlib import Path

from .cli import TERMINAL, validate_project
from .config import Provider
from .pal_bridge import auth_status, execute, sanitized_environment, selected_api_environment
from .security import sanitize


class SafeStream:
    """Persist complete sanitized lines so keys split between reads cannot leak."""
    def __init__(self, path, secrets=()):
        self.path = Path(path)
        self.secrets = tuple(value for value in secrets if value)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.file = os.fdopen(fd, "w", encoding="utf-8")

    def feed(self, chunk):
        self.pending += self.decoder.decode(chunk)
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.file.write(sanitize(line, self.secrets) + "\n")
            self.file.flush()

    def close(self):
        if self.file.closed:
            return
        self.pending += self.decoder.decode(b"", final=True)
        if self.pending:
            self.file.write(sanitize(self.pending, self.secrets))
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        self.pending = ""


def _directory(store, job_id):
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError("Invalid job identifier")
    root = store.state_dir / "jobs"
    if root.is_symlink():
        raise ValueError("CLI result directory must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / job_id
    if path.is_symlink():
        raise ValueError("CLI job directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _changed_artifacts(output, project):
    result = []
    for event in output.parsed.metadata.get("events", []):
        item = event.get("item") or {}
        if item.get("type") == "file_change":
            for change in item.get("changes", []):
                path = change.get("path")
                if isinstance(path, str):
                    resolved = (project / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
                    if resolved == project or resolved.is_relative_to(project):
                        result.append({"path": str(resolved), "type": "project_file", "kind": change.get("kind")})
    return result


async def run_job(store, vault, job_id):
    job = store.job(job_id)
    if not job or job.get("kind") != "cli":
        raise ValueError("Unknown CLI job")
    if job["status"] in TERMINAL:
        return job
    request = job["request"]
    streams = {}
    secrets = []
    began = time.monotonic()
    cancelled = asyncio.Event()
    result_dir = None
    watch = None
    run = None
    try:
        project = validate_project(request["project"], {"pair": {"projectRoots": request["projectRoots"]}})
        result_dir = _directory(store, job_id)
        if job.get("cancelRequested"):
            store.update_job(job_id, status="cancelled", error=None)
            return store.job(job_id)
        env = sanitized_environment()
        if request["mode"] == "api":
            if request.get("limits", {}).get("budgetUsd") is not None:
                raise ValueError("A finite hard API budget is not enforceable by official CLI delegation")
            provider = Provider.model_validate(request["provider"]).model_dump()
            if provider["id"] != request["providerId"] or not provider["enabled"]:
                raise ValueError("Selected API provider is unavailable")
            key = vault.get(request["providerId"])
            if not key:
                raise ValueError("Selected API key is missing in OS secure storage")
            secrets.append(key)
            escaped = json.dumps(key, ensure_ascii=True)[1:-1]
            if escaped != key:
                secrets.append(escaped)
            env.update(selected_api_environment(request["agent"], provider, key))
            if request["agent"] == "claude":
                # No invisible repeat of a potentially charged failed request.
                env["CLAUDE_CODE_MAX_RETRIES"] = "0"
        else:
            auth = await auth_status(request["agent"], request["executable"])
            if not auth.get("loggedIn") or auth.get("billingMode") != "subscription":
                raise ValueError("Official subscription authentication is unavailable; no API fallback was made")
        if request["agent"] == "claude" and request.get("limits", {}).get("maxTokens") is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request["limits"]["maxTokens"])
        if request["agent"] == "claude":
            # Native MCP subprocesses receive their safe baseline plus explicit
            # user-configured env, not our selected gateway key from ambient env.
            env["CLAUDE_CODE_MCP_ALLOWLIST_ENV"] = "1"
        for name in ("stdout", "stderr"):
            streams[name] = SafeStream(result_dir / (name + ".txt"), secrets)
        store.update_job(job_id, status="running", pid=os.getpid(), startedAt=time.time())
        store.event(job_id, "running", {"agent": request["agent"], "billingMode": request["mode"], "controls": request["controls"]})

        def spawned(process):
            store.update_job(job_id, nativePid=process.pid)

        def output(name, data):
            streams[name].feed(data)

        async def cancellation_watch():
            while not cancelled.is_set():
                current = store.job(job_id)
                if current and current.get("cancelRequested"):
                    cancelled.set()
                    store.update_job(job_id, status="cancelling")
                    run.cancel()
                    return
                await asyncio.sleep(0.15)

        loop = asyncio.get_running_loop()
        if os.name == "posix":
            def stop():
                cancelled.set()
                if run:
                    run.cancel()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop)
        run = asyncio.create_task(execute(request, environment=env, on_spawn=spawned, on_output=output))
        watch = asyncio.create_task(cancellation_watch())
        raw = await run
        for stream in streams.values():
            stream.close()
        metadata = raw.parsed.metadata
        failed = raw.returncode != 0 or metadata.get("is_error") or metadata.get("permission_denials") or metadata.get("errors")
        failed = failed or any(event.get("type") == "turn.failed" for event in metadata.get("events", []))
        status = "failed" if failed else "completed"
        if cancelled.is_set() or (store.job(job_id) or {}).get("cancelRequested"):
            status = "cancelled"
        actual_model = metadata.get("model_used")
        models_used = list(metadata.get("model_usage", {}))
        if len(models_used) > 1:
            actual_model = None
        warnings = list(request["controls"]["warnings"])
        if not actual_model:
            warnings.append("Actual upstream model was not reported by native CLI; selectedModel is a request, not verified identity")
        session_id = metadata.get("session_id")
        if not session_id:
            session_id = next((event.get("thread_id") for event in metadata.get("events", []) if event.get("type") == "thread.started"), None)
        result = {"text": raw.parsed.content, "agent": request["agent"], "billingMode": request["mode"],
                  "providerId": request.get("providerId"),
                  "selectedModel": request.get("model"), "model": actual_model, "modelsUsed": models_used or None,
                  "effort": request.get("effort"), "usage": metadata.get("usage"), "nativeSessionId": session_id,
                  "durationSeconds": round(time.monotonic() - began, 3), "returnCode": raw.returncode,
                  "controls": request["controls"], "warnings": warnings,
                  "artifacts": _changed_artifacts(raw, project), "metadata": metadata,
                  "stdout": raw.stdout, "stderr": raw.stderr,
                  "costUsd": 0.0 if request["mode"] == "subscription" else None,
                  "reservedUsd": 0.0 if request["mode"] == "subscription" else None}
        for field in ("nativeFailure", "nativeTurnId", "nativeTurnStatus", "usefulOutput"):
            if field in metadata:
                result[field] = metadata[field]
        native_estimate = (metadata.get("raw") or {}).get("total_cost_usd") if isinstance(metadata.get("raw"), dict) else None
        if isinstance(native_estimate, (int, float)) and math.isfinite(native_estimate) and native_estimate >= 0:
            result["nativeEstimatedCostUsd"] = native_estimate
        result = sanitize(result, secrets)
        _write_json(result_dir / "result.json", result)
        result["artifacts"] += [{"path": str(result_dir / name), "type": "cli_result"}
                                for name in ("stdout.txt", "stderr.txt", "result.json")]
        store.update_job(job_id, status=status, result=result, error="Native CLI reported an error or permission denial; full evidence retained" if failed else None,
                         costUsd=result["costUsd"], reservedUsd=result["reservedUsd"], completedAt=time.time())
        store.event(job_id, status, {"returnCode": raw.returncode, "resultPath": str(result_dir / "result.json")})
    except asyncio.CancelledError:
        store.update_job(job_id, status="cancelled", error=None, completedAt=time.time(),
                         costUsd=0.0 if request.get("mode") == "subscription" else None,
                         reservedUsd=0.0 if request.get("mode") == "subscription" else None)
        store.event(job_id, "cancelled", {"partialResultsPath": str(result_dir) if result_dir else None})
    except Exception as exc:
        from .pal_bridge import _load_vendor
        _load_vendor()
        from pair_core._pal_vendor.agents.base import CLIAgentError
        timeout = isinstance(exc, CLIAgentError) and "configured time limit" in str(exc)
        status = "timed_out" if timeout else "failed"
        error = sanitize(str(exc), secrets)
        partial_text = getattr(exc, "partial_text", "") if isinstance(exc, CLIAgentError) else ""
        partial_metadata = getattr(exc, "partial_metadata", {}) if isinstance(exc, CLIAgentError) else {}
        result = {"text": sanitize(partial_text, secrets), "agent": request.get("agent"), "selectedModel": request.get("model"), "model": None,
                  "billingMode": request.get("mode"), "error": error, "controls": request.get("controls"),
                  "metadata": sanitize(partial_metadata, secrets),
                  "artifacts": [{"path": str(result_dir / name), "type": "partial_cli_result"} for name in ("stdout.txt", "stderr.txt")]
                  if result_dir else []}
        store.update_job(job_id, status=status, error=error, result=result, completedAt=time.time(),
                         costUsd=0.0 if request.get("mode") == "subscription" else None,
                         reservedUsd=0.0 if request.get("mode") == "subscription" else None)
        store.event(job_id, status, {"error": error})
    finally:
        if watch:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        for stream in streams.values():
            stream.close()
        if os.name == "posix":
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().remove_signal_handler(sig)
    return store.job(job_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one already-validated Pair CLI job")
    parser.add_argument("--state", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    from .store import Store
    from .vault import Vault
    store = Store(Path(args.state))
    try:
        asyncio.run(run_job(store, Vault(store.state_dir), args.job))
    except Exception:
        # No raw exception/stdout fallback: errors may contain provider material.
        if store.job(args.job):
            store.update_job(args.job, status="failed", error="CLI worker startup failed; inspect Pair job configuration")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
