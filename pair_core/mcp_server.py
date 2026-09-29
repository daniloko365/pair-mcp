"""Official MCP SDK stdio facade; shares the native backend, not a second engine."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .security import sanitize


def clean_environment():
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME", "USER", "LOGNAME", "SSL_CERT_FILE", "SSL_CERT_DIR", "PAIR_KEYCHAIN_HELPER"}
    return {key: value for key, value in os.environ.items() if key in allowed}


class LocalBackend:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.endpoint = None
        self.token = None

    def descriptor(self):
        path = self.state_dir / "runtime.json"
        try:
            if path.stat().st_mode & 0o077:
                raise RuntimeError("Local runtime descriptor must be owner-only")
            data = json.loads(path.read_text())
            if not isinstance(data.get("pid"), int) or not isinstance(data.get("port"), int) or not isinstance(data.get("token"), str):
                return None
            os.kill(data["pid"], 0)
            # An existing PID is not evidence that it is still our backend.
            identity = subprocess.run(["/bin/ps", "-p", str(data["pid"]), "-o", "command="], capture_output=True, text=True, timeout=3, check=False)
            if identity.returncode:
                return None
            command = identity.stdout.strip()
            supplied_state = command.partition("--state ")[2].split(" --", 1)[0].strip().strip('"\'')
            try:
                exact_state = Path(supplied_state).expanduser().resolve() == self.state_dir
            except (ValueError, OSError):
                exact_state = False
            if "app-server" not in command or not exact_state or not ("pair_core" in command or "PairCore" in command):
                return None
            if not (1 <= data["port"] <= 65535):
                return None
            return data
        except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError):
            return None

    async def ensure(self):
        data = self.descriptor()
        if data is None:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            log = open(self.state_dir / "backend.log", "a")
            os.chmod(self.state_dir / "backend.log", 0o600)
            prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "pair_core"]
            child = subprocess.Popen(prefix + ["app-server", "--state", str(self.state_dir), "--port", "0"], cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, env=clean_environment(), start_new_session=True)
            log.close()
            for _ in range(100):
                data = self.descriptor()
                if data:
                    break
                if child.poll() is not None and child.returncode != 0:
                    raise RuntimeError("Pair backend failed to start; inspect sanitized local diagnostics")
                await asyncio.sleep(0.1)
            if data is None:
                raise RuntimeError("Pair startup is still pending; do not launch a duplicate backend")
        self.endpoint = f"http://127.0.0.1:{data['port']}"
        self.token = data["token"]
        from .build_info import get_build_info
        expected = get_build_info().get("sourceFingerprint")
        if not expected or data.get("build", {}).get("sourceFingerprint") != expected:
            raise RuntimeError("A different Pair backend is running. Finish its jobs before updating; no task was interrupted")

    async def request(self, path, method="GET", body=None):
        await self.ensure()
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, self.endpoint + path, json=body, headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        data = response.json()
        if response.status_code >= 400:
            raise RuntimeError(sanitize(data.get("message") or data.get("error") or f"Local operation failed ({response.status_code})"))
        return sanitize(data)


def build_server(state_dir):
    backend = LocalBackend(state_dir)
    server = MCPServer("Pair", version=__version__, instructions="Use saved council settings and official partner agents. Keep API calls and subscription jobs distinct. Missing quota/key data is unknown, not zero. Tools return actual evidence; host should synthesize council opinions when synthesis is delegated to the chat. Settings belong to the native Pair panel.")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def pair_status() -> dict:
        """Read Pair connections, native agent authentication, available skills and known quotas. No credential values."""
        data = await backend.request("/state")
        agents = {}
        for name in ("codex", "claude"):
            full = data.get("agents", {}).get(name, {})
            agents[name] = {k: v for k, v in full.items() if k != "skills"}
            agents[name]["skillCount"] = len(full["skills"]) if isinstance(full.get("skills"), list) else None
        return {"build": data.get("build"), "agents": agents, "providers": [{**p, "keyPresent": data.get("keyPresent", {}).get(p["id"], False)} for p in data["config"]["providers"]], "members": data["config"]["members"], "revision": data["config"]["revision"], "limits": data["config"]["limits"], "jev": data["config"]["jev"], "jobs": [{k: j.get(k) for k in ("id", "kind", "status", "createdAt", "costUsd", "reservedUsd")} for j in data.get("jobs", [])]}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def pair_skills(agent: str = "codex", query: str = "") -> dict:
        """Read/search native discovered skill metadata only when needed; never reads credential files or raw transcripts. Status gives counts to avoid injecting the whole library each turn."""
        if agent not in {"codex", "claude"}:
            raise ValueError("Unknown official agent")
        data = await backend.request("/agents")
        skills = data.get(agent, {}).get("skills")
        if not isinstance(skills, list):
            return {"agent": agent, "known": False, "skills": [], "message": "Native skill metadata is unavailable"}
        wanted = query.casefold()
        selected = [s for s in skills if not wanted or wanted in ((s.get("name") or "") + " " + (s.get("description") or "")).casefold()]
        return {"agent": agent, "known": True, "total": len(skills), "skills": selected}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def pair_council(question: str, context: str = "", project: str | None = None) -> dict:
        """Start a real council using saved members and fallbacks; API usage may cost money. Returns durable job ID; poll pair_job, then synthesize actual opinions in the chat if requested."""
        body = {"kind": "council", "question": question, "context": context}
        if project is not None:
            body["project"] = project
        return await backend.request("/jobs", "POST", body)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    async def pair_delegate(agent: str, prompt: str, project: str, model: str | None = None, effort: str | None = None, mode: str = "subscription", providerId: str | None = None, permission: str = "workspace-write") -> dict:
        """Delegate to official codex/claude CLI in an approved project. Poll pair_job to terminal before a reviewer handoff; use permission='read-only' for review. Concurrent writers in overlapping project directories are refused. May edit files/use quota or API budget; native permissions apply. The working directory is not a hardened whole-host sandbox."""
        data = await backend.request("/state")
        return await backend.request("/jobs", "POST", {"kind": "cli", "agent": agent, "prompt": prompt, "project": project, "model": model, "effort": effort, "mode": mode, "providerId": providerId, "permission": permission, "limits": data["config"]["limits"]})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def pair_job(job_id: str) -> dict:
        """Read a council or delegated task's real status/full result. Running is not completion; don't restart after an observation timeout."""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise ValueError("Invalid job ID")
        return await backend.request("/jobs/" + job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def pair_cancel(job_id: str) -> dict:
        """Cancel a specific running Pair job only when the user asks. Upstream API charges may remain unknown."""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise ValueError("Invalid job ID")
        return await backend.request("/jobs/" + job_id + "/cancel", "POST", {})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def pair_jev(operation: str, state: str, candidates: list | None = None, question: str | None = None) -> dict:
        """Opt-in Jev typed checks/ranking, not text generation. check requires question; rank requires candidates and uses optional question as ranking instructions. state supplies the task/evidence. Requires configured key; preserves raw evidence, uncertainty and usage. Never disables mandatory/explicit skills."""
        body = {"operation": operation, "state": state}
        for key, value in {"candidates": candidates, "question": question}.items():
            if value is not None:
                body[key] = value
        return await backend.request("/jev", "POST", body)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def pair_open_settings() -> dict:
        """Open the small native Pair settings panel. Does not open a website or expose credentials."""
        executable = Path(sys.executable).resolve()
        bundle = next((p for p in executable.parents if p.suffix == ".app" and (p / "Contents" / "Info.plist").is_file()), None)
        if bundle is None:
            bundle = Path(__file__).resolve().parent.parent / "dist" / "Pair.app"
        if not bundle.is_dir():
            return {"opened": False, "message": "Native app is not built yet"}
        process = await asyncio.create_subprocess_exec("/usr/bin/open", str(bundle), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return {"opened": await process.wait() == 0}

    return server


def run(state_dir):
    build_server(state_dir).run(transport="stdio")
