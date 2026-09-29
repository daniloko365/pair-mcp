"""Official CLI jobs, explicitly scoped and durably persisted by Pair."""
from __future__ import annotations

import asyncio
import copy
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import Limits, Provider
from .pal_bridge import auth_status, codex_metadata, probe, sanitized_environment, supports_api_provider, openrouter_claude_provider
from .store import ConflictError

TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode"}


def validate_project(project, config):
    path = Path(project).expanduser().resolve(strict=True)
    roots = [Path(root).expanduser().resolve(strict=True) for root in config.get("pair", {}).get("projectRoots", [])]
    if not path.is_dir() or not any(path == root or path.is_relative_to(root) for root in roots):
        raise ValueError("Project must be within an explicitly approved project directory")
    return path


def discover_executable(agent):
    found = shutil.which(agent)
    if found:
        return str(Path(found).resolve())
    if agent == "claude":
        local = Path.home() / ".local" / "bin" / "claude"
        if local.is_file() and os.access(local, os.X_OK):
            return str(local.resolve())
    if agent == "codex" and sys.platform == "darwin":
        for candidate in ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                          "/Applications/Codex.app/Contents/Resources/codex"]:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def control_disclosure(agent, mode, limits):
    warnings = []
    if limits.get("maxTokens") is not None:
        if agent == "claude":
            warnings.append("Claude native output-token ceiling applies to most requests; native model caps and exceptions remain")
        else:
            warnings.append("Official Codex CLI has no supported hard output-token parameter; requested token limit is not enforced")
    if mode == "subscription":
        warnings.append("Native subscription quotas apply; $0 API spend does not mean zero subscription usage")
    else:
        warnings.append("API CLI cost may be unknown; finite hard-dollar budgets must use the API council instead")
        if agent == "claude":
            warnings.append("Claude print mode supports --max-budget-usd as native estimated-spend control, not a guaranteed custom-provider invoice cap; Pair does not confuse the two")
        if agent == "codex":
            warnings.append("Codex API delegation requires this configured endpoint to support the Responses API")
            warnings.append("Codex API-mode foreign MCP environment isolation is unverified; no complete zero-leak guarantee is claimed")
    return {"timeSeconds": limits.get("timeSeconds"), "maxTokensRequested": limits.get("maxTokens"),
            "maxTokensEnforced": agent == "claude" and limits.get("maxTokens") is not None,
            "maxTokensScope": "native-most-requests" if agent == "claude" else "unsupported",
            "budgetUsdRequested": limits.get("budgetUsd"),
            "apiBudgetEnforced": mode == "subscription", "nativeEstimatedApiBudgetSupported": agent == "claude",
            "nativeEstimatedApiBudgetActive": False, "stepLimit": None, "warnings": warnings}


def _limits(config, overrides):
    combined = dict(config.get("limits", {}))
    if overrides is not None:
        combined.update(overrides)
    parsed = Limits.model_validate(combined).model_dump()
    if any(value is not None and not math.isfinite(float(value)) for value in parsed.values()):
        raise ValueError("Limits must be finite numbers or null")
    return parsed


class CliManager:
    def __init__(self, store, vault, *, executables=None, worker_launcher=None):
        self.store, self.vault = store, vault
        self.executables = dict(executables) if executables is not None else {}
        self.worker_launcher = worker_launcher
        self._cache = None
        self._cache_revision = None

    def _executable(self, agent):
        return self.executables.get(agent) if agent in self.executables else discover_executable(agent)

    async def status(self):
        await asyncio.to_thread(self.reconcile)
        cfg = self.store.config()
        now = time.time()
        if self._cache and self._cache_revision == cfg.get("revision") and now - self._cache["observedAt"] < 30:
            return copy.deepcopy(self._cache)

        async def inspect(agent):
            executable = self._executable(agent)
            status = {"installed": bool(executable), "executable": executable, "loggedIn": None,
                      "billingMode": None, "version": None, "skills": None, "quotas": None,
                      "models": None, "observedAt": time.time(), "warnings": [],
                      "capabilities": {"hardOutputTokens": False, "hardApiBudget": False,
                                       "nativeOutputTokens": agent == "claude", "nativeEstimatedApiBudget": None,
                                       "userTimeLimit": True, "nativePermissions": True}}
            if not executable:
                status["warnings"] = ["Official CLI is not installed; install and sign in through its own application"]
                return status
            status.update(await auth_status(agent, executable))
            try:
                rc, stdout, _ = await probe(executable, ["--version"])
                status["version"] = stdout.strip() if rc == 0 else None
            except (OSError, asyncio.TimeoutError):
                pass
            if agent == "codex":
                try:
                    data = await asyncio.wait_for(codex_metadata(executable, cfg["pair"]["projectRoots"]), 20)
                    account = (data.get("account") or {}).get("account") or {}
                    if account.get("type") == "chatgpt":
                        status.update(loggedIn=True, billingMode="subscription", planType=account.get("planType"))
                    limits = data.get("limits")
                    if limits:
                        buckets = limits.get("rateLimitsByLimitId") or {"codex": limits.get("rateLimits")}
                        quotas = []
                        for bucket_id, bucket in buckets.items():
                            if not isinstance(bucket, dict):
                                continue
                            for window in ("primary", "secondary"):
                                value = bucket.get(window)
                                if not isinstance(value, dict):
                                    continue
                                used = value.get("usedPercent")
                                known = isinstance(used, (int, float)) and math.isfinite(used)
                                quotas.append({"id": bucket_id, "window": window, "usedPercent": used if known else None,
                                               "remainingPercent": max(0, min(100, 100 - used)) if known else None,
                                               "resetsAt": value.get("resetsAt"), "windowDurationMins": value.get("windowDurationMins"),
                                               "known": known})
                        status["quotas"] = quotas or None
                    if data.get("skills"):
                        status["skills"] = [{key: skill.get(key) for key in ("name", "description", "enabled", "path")}
                                            for group in data["skills"].get("data", []) for skill in group.get("skills", [])]
                    if data.get("models"):
                        status["models"] = [{"id": model.get("model", model.get("id")),
                                             "name": model.get("displayName", model.get("model")),
                                             "reasoning": model.get("supportedReasoningEfforts")}
                                            for model in data["models"].get("data", [])]
                except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                    status["warnings"].append("Official Codex metadata unavailable; quotas/skills remain unknown")
            else:
                try:
                    rc, help_text, _ = await probe(executable, ["--help"])
                    status["capabilities"]["nativeEstimatedApiBudget"] = "--max-budget-usd" in help_text if rc == 0 else None
                except (OSError, asyncio.TimeoutError):
                    pass
                status["warnings"].append("Claude quotas/skills are unknown until exposed by its native session; no credential/transcript scanning")
            status["observedAt"] = time.time()
            return status

        codex, claude = await asyncio.gather(inspect("codex"), inspect("claude"))
        self._cache = {"codex": codex, "claude": claude, "observedAt": time.time(), "ttlSeconds": 30}
        self._cache_revision = cfg.get("revision")
        return copy.deepcopy(self._cache)

    def reconcile(self):
        """Recover only jobs whose owned worker is proven missing/replaced.

        No elapsed-time heuristic, process signalling, or automatic provider retry.
        An unregistered queued job is closed only after its launcher is gone.
        """
        for job in self.store.history():
            if job.get("kind") != "cli" or job.get("status") in TERMINAL:
                continue
            pid = job.get("pid")
            evidence = None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                if job.get("status") == "queued":
                    launcher = job.get("launcherPid")
                    if not isinstance(launcher, int) or isinstance(launcher, bool) or launcher <= 0:
                        continue  # Legacy job has no exact launcher evidence.
                    try:
                        os.kill(launcher, 0)
                    except ProcessLookupError:
                        evidence = "Queued CLI launcher no longer exists; no worker PID was registered"
                    except (PermissionError, OSError):
                        continue
                    else:
                        continue  # A live launcher can still register its worker.
                    current, changed = self.store.resolve_cli_without_pid(job["id"], status="interrupted", error=evidence)
                    if changed:
                        try:
                            self.store.event(job["id"], "interrupted", {"evidence": evidence, "automaticRetry": False})
                        except Exception:
                            pass  # The terminal reservation is already durable.
                    continue
                evidence = "Running job has no persisted worker PID"
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    evidence = "Persisted worker PID no longer exists"
                except (PermissionError, OSError):
                    continue  # Unknown, never fabricate a stopped/running process.
                if evidence is None and os.name == "posix":
                    try:
                        process = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                                 text=True, timeout=2, check=False, env=sanitized_environment())
                    except (OSError, subprocess.TimeoutExpired):
                        continue
                    command = process.stdout.strip()
                    if process.returncode and not command:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            evidence = "Persisted worker PID disappeared during identity check"
                        except OSError:
                            pass
                    elif command:
                        identifier = re.search(r"(?:^|\s)--job(?:\s+|=)" + re.escape(job["id"]) + r"(?:\s|$)", command)
                        owned = ("pair_core.cli_worker" in command or "cli-worker" in command) and identifier
                        if not owned:
                            evidence = "PID was reused or does not identify this Pair CLI worker/job"
            if evidence is None:
                continue
            current = self.store.job(job["id"])
            if not current or current.get("status") in TERMINAL:
                continue
            try:
                valid_id = str(uuid.UUID(job["id"])) == job["id"]
            except (ValueError, TypeError):
                valid_id = False
            partial = self.store.state_dir / "jobs" / job["id"] if valid_id else None
            artifacts = []
            if partial and not partial.is_symlink() and partial.is_dir():
                artifacts = [{"path": str(partial / name), "type": "partial_cli_result"}
                             for name in ("stdout.txt", "stderr.txt", "result.json") if (partial / name).is_file()]
            subscription = current.get("request", {}).get("mode") == "subscription"
            result = current.get("result") or {"text": "", "model": None, "artifacts": artifacts,
                                               "warning": "Worker interrupted; partial evidence retained. No automatic retry."}
            try:
                self.store.update_job(job["id"], status="interrupted", error=evidence, result=result,
                                      costUsd=0.0 if subscription else None, reservedUsd=0.0 if subscription else None)
            except ConflictError:
                continue  # The worker reached a terminal result during reconciliation.
            self.store.event(job["id"], "interrupted", {"evidence": evidence, "pid": pid, "automaticRetry": False})

    async def start(self, agent, prompt, project, model=None, effort=None, mode="subscription", providerId=None,
                    limits=None, permission=None, nativeEstimatedBudgetUsd=None, tools=None, _council_opinion=False):
        if agent not in {"codex", "claude"} or mode not in {"subscription", "api"}:
            raise ValueError("Choose official Codex or Claude Code and subscription or API mode")
        if nativeEstimatedBudgetUsd is not None and (agent != "claude" or mode != "api" or
                isinstance(nativeEstimatedBudgetUsd, bool) or not isinstance(nativeEstimatedBudgetUsd, (int, float)) or
                not math.isfinite(nativeEstimatedBudgetUsd) or nativeEstimatedBudgetUsd <= 0):
            raise ValueError("Native estimated spend control is only available for Claude API jobs with a positive finite value")
        if tools is not None and (agent != "claude" or not isinstance(tools, list) or any(tool not in {"Read", "Grep", "Glob", "Edit", "Write", "Bash"} for tool in tools)):
            raise ValueError("Explicit tool selection is only available for native Claude tools")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A task prompt is required")
        cfg = self.store.config()
        project_path = validate_project(project, cfg)
        executable = self._executable(agent)
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"Official {agent} CLI is not installed. Install it and sign in through its own flow")
        selected = cfg["pair"].get(agent, {})
        model = model or selected.get("model")
        effort = effort if effort is not None else selected.get("effort")
        if model is not None and (not isinstance(model, str) or not model or model.startswith("-") or len(model) > 300 or any(ord(ch) < 32 for ch in model)):
            raise ValueError("Invalid exact model ID")
        if effort is not None and effort not in EFFORTS:
            raise ValueError("Unsupported reasoning effort")
        if permission is None:
            permission = "workspace-write" if agent == "codex" else "default"
        if permission not in {"default", "read-only", "workspace-write", "accept-edits"} or (agent == "codex" and permission == "accept-edits"):
            raise ValueError("Unsupported native permission mode")
        if agent == "claude" and permission == "read-only" and tools is not None and not set(tools).issubset({"Read", "Glob", "Grep"}):
            raise ValueError("Read-only Claude review accepts only native file-read tools")
        resolved_limits = _limits(cfg, limits)
        provider = None
        if mode == "api":
            if resolved_limits.get("budgetUsd") is not None:
                raise ValueError("A finite hard API budget cannot be guaranteed by official CLI delegation; use the API council")
            provider = next((item for item in cfg["providers"] if item["id"] == providerId and item.get("enabled", True)), None)
            if not provider:
                raise ValueError("Select an enabled configured API provider")
            Provider.model_validate(provider)
            required_protocol = "openai" if agent == "codex" else "anthropic"
            if not supports_api_provider(agent, provider):
                raise ValueError(f"Official {agent} API mode requires {required_protocol} protocol; no token/protocol proxy is used")
            if not self.vault.has(providerId):
                raise ValueError("Selected provider API key is missing in OS secure storage")
        else:
            auth = await auth_status(agent, executable)
            if not auth.get("loggedIn") or auth.get("billingMode") != "subscription":
                raise ValueError("Official subscription sign-in is unavailable or billing mode is unknown; sign in through the official CLI")
        request = {"agent": agent, "prompt": prompt, "project": str(project_path), "model": model, "effort": effort,
                   "mode": mode, "providerId": providerId if mode == "api" else None, "provider": provider,
                   "permission": permission, "limits": resolved_limits, "executable": str(Path(executable).resolve()),
                   "projectRoots": cfg["pair"]["projectRoots"], "configRevision": cfg["revision"],
                   "controls": control_disclosure(agent, mode, resolved_limits)}
        if agent == "claude" and permission == "read-only":
            request["controls"]["warnings"].append(
                "Claude reviewer uses project-scoped file-read tools only; its native CLI may still write private application state")
        if _council_opinion:
            if agent != "codex" or mode != "subscription" or permission != "read-only":
                raise ValueError("Typed native council transport is only available for read-only Codex subscription opinions")
            request["transport"] = "codex-app-server-opinion"
            request["controls"]["warnings"].append("Read-only native app-server opinion; only verified terminal capacity/quota errors can use saved council backups")
        if nativeEstimatedBudgetUsd is not None:
            request["nativeEstimatedBudgetUsd"] = float(nativeEstimatedBudgetUsd)
            request["controls"].update(nativeEstimatedApiBudgetActive=True, nativeEstimatedBudgetUsd=float(nativeEstimatedBudgetUsd))
        if tools is not None:
            request["tools"] = list(tools)
            request["controls"]["nativeTools"] = list(tools)
        if agent == "claude" and mode == "api" and openrouter_claude_provider(provider):
            request["controls"]["warnings"] += [
                "OpenRouter uses its native Anthropic-compatible endpoint and the existing selected key; no second account/key is created",
                "OpenRouter documents possible conflicts with a cached Claude login; Pair never logs out or deletes credentials. Verify the selected API source if native authentication warns",
                "OpenRouter only guarantees Claude Code compatibility with the Anthropic first-party provider"]
        job = self.store.create_job("cli", request)
        try:
            self.store.event(job["id"], "queued", {"agent": agent, "mode": mode, "project": str(project_path), "controls": request["controls"]})
            if self.worker_launcher:
                process = self.worker_launcher(job)
            else:
                package_root = Path(__file__).resolve().parents[1]
                env = sanitized_environment(worker=True)
                prefix = [sys.executable, "cli-worker"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "pair_core.cli_worker"]
                process = subprocess.Popen(prefix + ["--state", str(self.store.state_dir), "--job", job["id"]],
                                           cwd=package_root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"))
        except Exception:
            self.store.resolve_cli_without_pid(job["id"], status="failed",
                                               error="CLI worker could not start; no provider request was confirmed")
            raise RuntimeError("CLI worker could not start") from None
        try:
            self.store.update_job(job["id"], pid=process.pid)
        except Exception:
            # The detached worker can still persist its own PID and result.
            # Never claim a zero-charge failure or launch a replacement here.
            raise RuntimeError("CLI worker launched but job registration is unconfirmed; inspect this job before retrying") from None
        return self.store.job(job["id"])

    async def cancel(self, job_id):
        # The owned worker reads this durable flag and cancels its own PAL process.
        # Never signal an unverified persisted PID that could have been reused.
        job, event = self.store.request_cli_cancel(job_id)
        if event:
            try:
                self.store.event(job_id, event, {})
            except Exception:
                pass  # The cancellation state is already durable.
        return job
