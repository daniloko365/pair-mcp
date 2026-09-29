"""Thin adapter over the reviewed, pinned PAL CLI runner/parsers.

Never loads PAL presets, another user's config, credential files, or a shell.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .security import sanitize

REVISION = "7afc7c1cc96e23992c8f105f960132c657883bb1"
VENDOR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "vendor" / "pal" / "clink"
ENV_ALLOWED = {
    "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "COMSPEC", "PATHEXT", "SSL_CERT_FILE", "SSL_CERT_DIR",
}
PAIR_CODEX_POLICY = [
    # An absent server needs a valid transport to parse; replace only Pair's
    # per-run table, disabled, with a nonexecutable sentinel command.
    'mcp_servers.pair={enabled=false,command="pair-disabled-for-child"}',
    'plugins."pair-companion@personal".enabled=false',
    'plugins."pair-companion@personal".mcp_servers.pair.enabled=false',
    'plugins."pair-companion".mcp_servers.pair.enabled=false',
]
PAIR_CLAUDE_DENY = ["mcp__pair__*", "mcp__plugin_pair-companion_pair__*", "mcp__plugin_pair_companion_pair__*"]
PAIR_CLAUDE_REVIEW_TOOLS = ("Read", "Glob", "Grep")
PAIR_CLAUDE_REVIEW_DENY = ("Bash", "Edit", "Write", "NotebookEdit", "mcp__*")
CODEX_FAILURE_CODES = {"usageLimitExceeded": "partner_verified_usage_limit",
                       "rateLimitExceeded": "partner_verified_rate_limit",
                       "serverOverloaded": "partner_verified_capacity"}


def verified_partner_failure(job, agent):
    """Consume only our official structured adapter's persisted failure.

    Native error messages, stderr and assistant output are never classifiers.
    This is not a protection against a malicious same-user state-file editor.
    """
    request, result = job.get("request") or {}, job.get("result") or {}
    if not isinstance(request, dict) or not isinstance(result, dict):
        return None
    failure = result.get("nativeFailure")
    text = result.get("text", "")
    metadata = result.get("metadata") or {}
    if (agent != "codex" or job.get("status") != "failed" or job.get("cancelRequested") or
            request.get("agent") != agent or request.get("mode") != "subscription" or
            request.get("permission") != "read-only" or result.get("agent") != agent or
            request.get("transport") != "codex-app-server-opinion" or result.get("billingMode") != "subscription" or
            not isinstance(text, str) or text.strip() or result.get("usefulOutput") is not False or
            not isinstance(failure, dict) or failure.get("usefulOutput") is not False or
            failure.get("billingMode") != "subscription" or not isinstance(metadata, dict) or metadata.get("permission_denials")):
        return None
    if metadata.get("transport") != "codex-app-server-opinion" or metadata.get("nativeFailure") != failure:
        return None
    if failure.get("provenance") == "codex_app_server_turn_completed_v2":
        native_code = failure.get("nativeCode")
        expected = CODEX_FAILURE_CODES.get(native_code) if isinstance(native_code, str) else None
        if (failure.get("code") == expected and expected and failure.get("threadId") == result.get("nativeSessionId") and
                isinstance(failure.get("threadId"), str) and failure.get("threadId") and
                failure.get("turnId") == result.get("nativeTurnId") and isinstance(failure.get("turnId"), str) and
                failure.get("turnId") and result.get("nativeTurnStatus") == "failed"):
            return failure
    return None


def sanitized_environment(source=None, *, worker=False):
    """Allowlisting avoids forwarding secrets with innocent variable names."""
    source = os.environ if source is None else source
    allowed = ENV_ALLOWED | ({"PAIR_KEYCHAIN_HELPER"} if worker else set())
    return {key: value for key, value in source.items() if key in allowed and isinstance(value, str)}


def openrouter_claude_provider(provider):
    """Only the configured official host gets its documented Messages skin."""
    url = urlsplit(provider.get("baseUrl", ""))
    return url.scheme == "https" and url.hostname == "openrouter.ai" and provider.get("protocol") in {"openai", "anthropic"}


def supports_api_provider(agent, provider):
    return (agent == "codex" and provider.get("protocol") == "openai") or (
        agent == "claude" and (provider.get("protocol") == "anthropic" or openrouter_claude_provider(provider)))


def selected_api_environment(agent, provider, key):
    """Uses one explicit vault key; never copies auth or modifies host settings."""
    if not supports_api_provider(agent, provider):
        raise ValueError("Provider protocol is incompatible with this official CLI")
    if agent == "codex":
        return {"PAIR_SELECTED_API_KEY": key}
    if openrouter_claude_provider(provider):
        return {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api"}
    return {"ANTHROPIC_API_KEY": key, "ANTHROPIC_BASE_URL": provider["baseUrl"]}


def _load_vendor():
    name = "pair_core._pal_vendor"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, VENDOR / "__init__.py", submodule_search_locations=[str(VENDOR)])
        if spec is None or spec.loader is None:
            raise RuntimeError("Pinned PAL CLI subset is missing from this Pair installation")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)


def build_client(request, *, environment):
    _load_vendor()
    from pair_core._pal_vendor.models import ResolvedCLIClient, ResolvedCLIRole

    agent = request["agent"]
    permission = request.get("permission", "workspace-write" if agent == "codex" else "default")
    mode = request.get("mode", "subscription")
    args = []
    if agent == "codex":
        internal = ["exec"]
        args = ["--json", "--sandbox", "read-only" if permission == "read-only" else "workspace-write",
                "--skip-git-repo-check", "-c", 'approval_policy="on-request"',
                "-c", "sandbox_workspace_write.writable_roots=[]",
                "-c", 'shell_environment_policy.inherit="core"',
                "-c", "shell_environment_policy.ignore_default_excludes=false",
                "-c", "shell_environment_policy.set={}", "-c", "allow_login_shell=false"]
        for policy in PAIR_CODEX_POLICY:
            args += ["-c", policy]
        if mode == "subscription":
            args += ["-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"']
        else:
            provider = request["provider"]
            args += ["-c", 'model_provider="pair_api"', "-c", 'forced_login_method="api"',
                     "-c", 'model_providers.pair_api.name="Pair configured API"',
                     "-c", "model_providers.pair_api.base_url=" + json.dumps(provider["baseUrl"]),
                     "-c", 'model_providers.pair_api.env_key="PAIR_SELECTED_API_KEY"',
                     "-c", 'model_providers.pair_api.wire_api="responses"']
        if request.get("model"):
            args += ["--model", request["model"]]
        if request.get("effort"):
            args += ["-c", "model_reasoning_effort=" + json.dumps(request["effort"])]
        args.append("-")
        parser = "codex_jsonl"
    elif agent == "claude":
        internal = ["--print", "--output-format", "json"]
        # Plan mode may write ~/.claude/plans. A reviewer needs bounded project
        # reads, not a native planning session or arbitrary shell/MCP tools.
        native_permission = {"read-only": "dontAsk", "workspace-write": "acceptEdits", "accept-edits": "acceptEdits"}.get(permission, "default")
        deny = PAIR_CLAUDE_DENY + list(PAIR_CLAUDE_REVIEW_DENY if permission == "read-only" else ())
        settings = {"permissions": {"additionalDirectories": [], "defaultMode": native_permission,
                                    "deny": deny}}
        if mode == "subscription":
            settings["forceLoginMethod"] = "claudeai"
        args = ["--permission-mode", native_permission, "--settings", json.dumps(settings, separators=(",", ":"))]
        args += ["--disallowedTools", *deny]
        if permission == "read-only":
            # Restricted confines built-in file reads to the approved project,
            # discards user/project settings and removes command/Web tools.
            # Native CLI cache/auth state may still be written outside it.
            args += ["--restricted", "--strict-mcp-config", "--no-session-persistence"]
        if request.get("nativeEstimatedBudgetUsd") is not None:
            args += ["--max-budget-usd", str(request["nativeEstimatedBudgetUsd"])]
        if permission == "read-only":
            selected_tools = request.get("tools")
            if selected_tools is None:
                selected_tools = PAIR_CLAUDE_REVIEW_TOOLS
            if not set(selected_tools).issubset(PAIR_CLAUDE_REVIEW_TOOLS):
                raise ValueError("Read-only Claude review accepts only native file-read tools")
            args += ["--tools", ",".join(selected_tools)]
        elif request.get("tools") is not None:
            args += ["--tools", ",".join(request["tools"])]
        if request.get("model"):
            args += ["--model", request["model"]]
        if request.get("effort"):
            args += ["--effort", request["effort"]]
        parser = "claude_json"
    else:
        raise ValueError("Unknown official CLI")
    role = ResolvedCLIRole(name="pair", prompt_path=Path(request["project"]), role_args=[])
    return ResolvedCLIClient(name=agent, executable=[request["executable"]], working_dir=Path(request["project"]),
                             internal_args=internal, config_args=args, env=dict(environment),
                             timeout_seconds=request.get("limits", {}).get("timeSeconds"),
                             parser=parser, runner=agent, roles={"pair": role})


async def execute(request, *, environment, on_spawn=None, on_output=None):
    if request.get("transport") == "codex-app-server-opinion":
        if request.get("agent") != "codex" or request.get("mode") != "subscription" or request.get("permission") != "read-only":
            raise ValueError("Typed native opinion transport requires read-only Codex subscription mode")
        return await codex_opinion(request, environment=environment, on_spawn=on_spawn, on_output=on_output)
    client = build_client(request, environment=environment)
    from pair_core._pal_vendor.agents import create_agent
    return await create_agent(client).run(role=client.roles["pair"], prompt=request["prompt"],
                                          files=[], images=[], on_spawn=on_spawn, on_output=on_output)


async def probe(executable, args, *, timeout=8, environment=None):
    """Documented read-only CLI diagnostics only; never runs a model turn."""
    process = await asyncio.create_subprocess_exec(executable, *args, stdin=asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                   env=environment or sanitized_environment(), limit=1024 * 1024)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.communicate()
        raise
    return process.returncode, sanitize(stdout.decode("utf-8", errors="replace")), sanitize(stderr.decode("utf-8", errors="replace"))


async def auth_status(agent, executable):
    try:
        if agent == "codex":
            rc, stdout, stderr = await probe(executable, ["login", "status"])
            text = (stdout + stderr).lower()
            mode = "subscription" if "chatgpt" in text and rc == 0 else "api" if "api key" in text and rc == 0 else None
            return {"loggedIn": rc == 0, "billingMode": mode}
        rc, stdout, _ = await probe(executable, ["auth", "status"])
        value = json.loads(stdout)
        method = value.get("authMethod", value.get("auth_method"))
        mode = "subscription" if method in {"claude.ai", "claudeai", "oauth"} else "api" if method in {"api_key", "apiKey", "console"} else None
        return {"loggedIn": bool(value.get("loggedIn", value.get("logged_in", rc == 0))), "billingMode": mode}
    except (OSError, ValueError, asyncio.TimeoutError):
        return {"loggedIn": None, "billingMode": None, "warning": "Official CLI authentication status is unavailable"}


class _CodexRPC:
    """Shared official stdio framing for diagnostics and one read-only turn.

    No agent loop, retries, approval grants or authentication handling here.
    All server requests are denied and recorded; only notifications and replies
    from the owned native process carry machine-error authority.
    """
    def __init__(self, process, *, on_output=None, read_timeout=None):
        self.process, self.on_output, self.read_timeout = process, on_output, read_timeout
        self.notifications, self.denied_requests, self.lines = [], [], []
        self.stderr_chunks = []
        self.stderr_task = asyncio.create_task(self._drain_stderr()) if process.stderr is not None else None

    async def _drain_stderr(self):
        while chunk := await self.process.stderr.read(65536):
            self.stderr_chunks.append(chunk)
            if self.on_output:
                self.on_output("stderr", chunk)

    async def send(self, payload):
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), self.read_timeout)
        if not line:
            raise RuntimeError("Official app-server ended before its terminal response")
        self.lines.append(line.decode("utf-8", errors="replace"))
        if self.on_output:
            self.on_output("stdout", line)
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise RuntimeError("Official app-server returned an invalid packet")
        if "method" in payload and "id" in payload:
            self.denied_requests.append({"id": payload["id"], "method": payload["method"]})
            await self.send({"id": payload["id"], "error": {"code": -32601, "message": "Pair read-only adapter does not approve requests"}})
        return payload

    async def call(self, call_id, method, params=None):
        await self.send({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params or {}})
        while True:
            payload = await self.read()
            if payload.get("id") == call_id and "method" not in payload:
                if "error" in payload:
                    return None
                return payload.get("result")
            if "method" in payload and "id" not in payload:
                self.notifications.append(payload)

    async def close(self):
        if self.process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()
        if self.stderr_task:
            try:
                await asyncio.wait_for(self.stderr_task, 2)
            except asyncio.TimeoutError:
                self.stderr_task.cancel()
                await asyncio.gather(self.stderr_task, return_exceptions=True)


async def _codex_rpc(executable, *, environment=None, policies=(), on_spawn=None, on_output=None, read_timeout=None, cwd=None):
    args = [item for policy in [*PAIR_CODEX_POLICY, *policies] for item in ("-c", policy)]
    process = await asyncio.create_subprocess_exec(executable, "app-server", *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE if on_output else asyncio.subprocess.DEVNULL,
        env=environment or sanitized_environment(), limit=4 * 1024 * 1024, cwd=cwd, start_new_session=(os.name == "posix"))
    rpc = _CodexRPC(process, on_output=on_output, read_timeout=read_timeout)
    try:
        if on_spawn:
            on_spawn(process)
    except Exception:
        await rpc.close()
        raise
    return rpc


async def codex_metadata(executable, projects):
    """Read-only public app-server methods, not credential/transcript discovery."""
    rpc = await _codex_rpc(executable, read_timeout=8)
    try:
        await rpc.call(1, "initialize", {"clientInfo": {"name": "pair-status", "version": __version__}, "capabilities": {"experimentalApi": True}})
        await rpc.send({"method": "initialized"})
        account = await rpc.call(2, "account/read", {"refreshToken": False})
        limits = await rpc.call(3, "account/rateLimits/read")
        skills = await rpc.call(4, "skills/list", {"cwds": projects, "forceReload": False}) if projects else None
        models = await rpc.call(5, "model/list", {})
        return {"account": account, "limits": limits, "skills": skills, "models": models}
    finally:
        await rpc.close()


async def codex_opinion(request, *, environment, on_spawn=None, on_output=None):
    """Official native read-only council turn, with typed terminal failures.

    `codex exec --json` drops CodexErrorInfo (ThreadErrorEvent is message-only).
    The official app-server v2 schema retains it. Coding delegation still uses
    the pinned PAL exec runner; this adapter is council-opinion-only.
    Schema authority: codex-cli 0.158.0-alpha.2 generate-json-schema; docs:
    https://learn.chatgpt.com/docs/app-server
    """
    _load_vendor()
    from pair_core._pal_vendor.agents.base import AgentOutput, CLIAgentError
    from pair_core._pal_vendor.parsers.base import ParsedCLIResponse
    policies = ['model_provider="openai"', 'forced_login_method="chatgpt"', 'sandbox_mode="read-only"',
                'approval_policy="on-request"', 'shell_environment_policy.inherit="core"',
                'shell_environment_policy.set={}', 'shell_environment_policy.ignore_default_excludes=false',
                'allow_login_shell=false']
    rpc = await _codex_rpc(request["executable"], environment=environment, policies=policies,
                           on_spawn=on_spawn, on_output=on_output, cwd=request["project"])
    began, text, useful, thread_id, turn_id = time.monotonic(), {}, False, None, None
    disqualifying_error = False
    metadata = {"transport": "codex-app-server-opinion", "is_error": True}

    def consume_item(item):
        nonlocal useful
        if not isinstance(item, dict):
            raise RuntimeError("Native item is malformed; no API fallback")
        kind = item.get("type")
        if not isinstance(kind, str) or not kind:
            raise RuntimeError("Native item type is malformed; no API fallback")
        if kind == "agentMessage":
            content, identity = item.get("text"), item.get("id", "message")
            if not isinstance(content, str) or not isinstance(identity, str):
                raise RuntimeError("Native message is malformed; no API fallback")
            text[identity] = content
            useful = useful or bool(content.strip())
        elif kind == "reasoning":
            parts = [item.get("summary", []), item.get("content", [])]
            if any(not isinstance(part, list) for part in parts):
                raise RuntimeError("Native reasoning is malformed; no API fallback")
            useful = useful or any(str(part).strip() for group in parts for part in group)
        elif kind not in {"userMessage", "error", None}:
            # A started native tool could already have acted; do not replay it.
            useful = True
    try:
        async with asyncio.timeout(request.get("limits", {}).get("timeSeconds")):
            initialized = await rpc.call(1, "initialize", {"clientInfo": {"name": "pair-council", "version": __version__},
                                                           "capabilities": {"experimentalApi": True}})
            if initialized is None:
                raise RuntimeError("Official native initialization failed; no API fallback")
            await rpc.send({"method": "initialized"})
            account = await rpc.call(2, "account/read", {"refreshToken": False})
            if not isinstance(account, dict) or (account.get("account") or {}).get("type") != "chatgpt":
                raise RuntimeError("Native subscription account is unavailable; no API fallback")
            # Display quotas are not a routing decision: bucket/model and extra
            # usage applicability are not proven by usedPercent/normalModelSlug.
            started = await rpc.call(4, "thread/start", {"model": request.get("model"), "modelProvider": "openai",
                "cwd": request["project"], "sandbox": "read-only", "approvalPolicy": "on-request",
                "ephemeral": True, "allowProviderModelFallback": False})
            if (not isinstance(started, dict) or (started.get("sandbox") or {}).get("type") != "readOnly" or
                    started.get("modelProvider") != "openai"):
                raise RuntimeError("Native read-only subscription policy could not be verified; no API fallback")
            thread_id = (started.get("thread") or {}).get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Native thread identity unavailable; no API fallback")
            turn = await rpc.call(5, "turn/start", {"threadId": thread_id,
                "input": [{"type": "text", "text": request["prompt"]}], "effort": request.get("effort")})
            turn_id = ((turn or {}).get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise RuntimeError("Native turn identity unavailable; no API fallback")
            while True:
                packet = rpc.notifications.pop(0) if rpc.notifications else await rpc.read()
                params = packet.get("params") or {}
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    continue
                method = packet.get("method")
                same_turn = params.get("turnId") == turn_id
                if same_turn and method == "error":
                    error = params.get("error")
                    code = error.get("codexErrorInfo") if isinstance(error, dict) else None
                    disqualifying_error = disqualifying_error or not isinstance(code, str) or code not in CODEX_FAILURE_CODES
                if same_turn and method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str):
                        key = params.get("itemId", "message")
                        if not isinstance(key, str):
                            raise RuntimeError("Native message identity is malformed; no API fallback")
                        text[key] = text.get(key, "") + delta
                        useful = useful or bool(delta.strip())
                elif same_turn and method in {"item/started", "item/completed"}:
                    consume_item(params.get("item"))
                elif same_turn and method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
                    useful = useful or bool(str(params.get("delta") or "").strip())
                elif same_turn and method == "thread/tokenUsage/updated":
                    metadata["usage"] = params.get("tokenUsage")
                elif method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                    terminal = params["turn"]
                    items = terminal.get("items", [])
                    if not isinstance(items, list):
                        raise RuntimeError("Native terminal items are malformed; no API fallback")
                    for item in items:
                        consume_item(item)
                    metadata["nativeTurnStatus"] = terminal.get("status")
                    metadata["is_error"] = terminal.get("status") != "completed" or bool(rpc.denied_requests)
                    error = terminal.get("error")
                    code = error.get("codexErrorInfo") if isinstance(error, dict) else None
                    if terminal.get("status") == "failed" and isinstance(code, str) and code in CODEX_FAILURE_CODES and not useful and not rpc.denied_requests and not disqualifying_error:
                        metadata["nativeFailure"] = {"code": CODEX_FAILURE_CODES[code], "nativeCode": code,
                            "provenance": "codex_app_server_turn_completed_v2", "threadId": thread_id,
                            "turnId": turn_id, "billingMode": "subscription", "usefulOutput": False}
                    break
            metadata.update(session_id=thread_id, nativeTurnId=turn_id, usefulOutput=useful,
                            permission_denials=rpc.denied_requests, appServerEvents=[json.loads(line) for line in rpc.lines])
            return AgentOutput(parsed=ParsedCLIResponse(content="\n\n".join(text.values()), metadata=metadata),
                sanitized_command=[request["executable"], "app-server"], returncode=1 if metadata["is_error"] else 0,
                stdout="".join(rpc.lines), stderr=b"".join(rpc.stderr_chunks).decode("utf-8", errors="replace"),
                duration_seconds=time.monotonic()-began, parser_name="codex_app_server_v2")
    except Exception as exc:
        message = "Official CLI reached the configured time limit" if isinstance(exc, TimeoutError) else str(exc)
        error = CLIAgentError(message, stdout="".join(rpc.lines), stderr=b"".join(rpc.stderr_chunks).decode("utf-8", errors="replace"))
        error.partial_text = "\n\n".join(text.values())
        error.partial_metadata = {"transport": "codex-app-server-opinion", "usefulOutput": useful,
                                  "session_id": thread_id, "nativeTurnId": turn_id,
                                  "permission_denials": rpc.denied_requests}
        raise error from None
    finally:
        await rpc.close()
