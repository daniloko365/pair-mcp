"""One local admin service, reused by the native panel and stdio MCP proxy."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path

import uvicorn
from filelock import FileLock, Timeout as LockTimeout
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .security import sanitize, safe_error
from .store import Store, ConflictError
from .vault import Vault


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"} or request.headers.get("origin"):
            return JSONResponse({"error": "Only the native local client is allowed"}, status_code=403)
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth, "Bearer " + self.token):
            return JSONResponse({"error": "Local authorization required"}, status_code=401)
        if request.method in {"POST", "PUT", "PATCH"} and request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "JSON required"}, status_code=415)
        return await call_next(request)


class FrozenStore:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, copy.deepcopy(cfg)

    def config(self):
        return copy.deepcopy(self.cfg)

    def __getattr__(self, name):
        return getattr(self.store, name)


class Service:
    def __init__(self, store, vault, *, cli=None, catalog=None, council_factory=None):
        from .build_info import get_build_info
        self.build = get_build_info()
        self.store, self.vault = store, vault
        self._cli, self._catalog, self.council_factory = cli, catalog, council_factory
        self.tasks = {}
        self.status_cache = None
        self.status_lock = asyncio.Lock()

    @property
    def cli(self):
        if self._cli is None:
            from .cli import CliManager
            self._cli = CliManager(self.store, self.vault)
        return self._cli

    @property
    def catalog(self):
        if self._catalog is None:
            from .providers import Catalog
            self._catalog = Catalog(self.vault)
        return self._catalog

    async def body(self, request):
        if int(request.headers.get("content-length", "0")) > 1_000_000:
            raise ValueError("Request too large")
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise ValueError("Request too large")
        return json.loads(raw or "{}")

    async def state(self, request):
        from .build_info import get_build_info
        cfg = self.store.config()
        presence = {}
        for p in cfg["providers"]:
            try:
                presence[p["id"]] = self.vault.has(p["id"])
            except Exception:
                presence[p["id"]] = False
        try:
            agents = await self.agent_status()
        except Exception as exc:
            agents = {"error": safe_error(exc)}
        summaries = [{k: j.get(k) for k in ("id", "kind", "status", "createdAt", "updatedAt", "costUsd", "reservedUsd", "knownCostUsd", "error")} | {"request": {k: j.get("request", {}).get(k) for k in ("agent", "model", "mode", "project")}} for j in self.store.history()]
        return JSONResponse(sanitize({"config": cfg, "keyPresent": presence, "jobs": summaries, "agents": agents, "build": self.build}))

    async def config(self, request):
        cfg = self.store.save_config(await self.body(request))
        return JSONResponse({"config": cfg, "saved": True, "appliesTo": "next invocation"})

    def provider(self, request):
        pid = request.path_params["provider_id"]
        for p in self.store.config()["providers"]:
            if p["id"] == pid:
                return p
        raise KeyError("Unknown configured provider")

    async def key(self, request):
        provider = self.provider(request)
        if request.method == "DELETE":
            self.vault.delete(provider["id"])
        else:
            body = await self.body(request)
            if set(body) != {"key"}:
                raise ValueError("Only the key field is accepted")
            self.vault.set(provider["id"], body["key"])
        return JSONResponse({"keyPresent": self.vault.has(provider["id"])})

    async def models(self, request):
        import time
        return JSONResponse({"models": await self.catalog.models(self.provider(request)), "syncedAt": time.time()})

    async def agents(self, request):
        return JSONResponse(sanitize(await self.agent_status()))

    async def install(self, request):
        from .installer import install
        client = request.path_params["client"]
        if client not in {"codex", "claude"}:
            raise ValueError("Unsupported client")
        result = await asyncio.to_thread(install, client, self.store.state_dir)
        return JSONResponse(result)

    async def uninstall(self, request):
        from .installer import uninstall
        client = request.path_params["client"]
        if client not in {"codex", "claude"}:
            raise ValueError("Unsupported client")
        return JSONResponse(await asyncio.to_thread(uninstall, client, self.store.state_dir))

    async def agent_status(self):
        async with self.status_lock:
            if self.status_cache and time.monotonic() - self.status_cache[0] < 15:
                return self.status_cache[1]
            value = sanitize(await self.cli.status())
            self.status_cache = (time.monotonic(), value)
            return value

    async def jobs(self, request):
        body = await self.body(request)
        kind = body.pop("kind", "cli")
        if kind == "cli":
            allowed = {"agent", "prompt", "project", "model", "effort", "mode", "providerId", "limits", "permission"}
            if set(body) - allowed:
                raise ValueError("Unknown job field")
            return JSONResponse(sanitize(await self.cli.start(**body)), status_code=202)
        if kind != "council" or set(body) - {"question", "context", "members", "limits", "project"}:
            raise ValueError("Invalid council job")
        if not isinstance(body.get("question"), str) or not body["question"].strip():
            raise ValueError("Question is required")
        cfg = self.store.config()
        job = self.store.create_job("council", {**body, "configSnapshot": cfg})
        task = asyncio.create_task(self.run_council(job["id"], body, cfg))
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return JSONResponse(job, status_code=202)

    async def run_council(self, job_id, body, cfg):
        self.store.update_job(job_id, status="running", pid=os.getpid())
        self.store.event(job_id, "started", {"revision": cfg["revision"]})
        runner = None
        attempts = {}

        async def record(event):
            kind = event.get("type", "progress")
            # No raw SSE deltas in durable events. Routing emits final sanitized evidence.
            if not kind.startswith("attempt_") and kind not in {"member_complete", "member_error", "council_cancelled"}:
                return
            safe = sanitize(event)
            self.store.event(job_id, kind, safe)
            aid = safe.get("attemptId")
            if aid:
                attempts[aid] = {**attempts.get(aid, {}), **safe}
                reserve = sum(float(a.get("reservedUsd") or 0) for a in attempts.values())
                known = sum(float(a.get("costUsd") or 0) for a in attempts.values())
                self.store.update_job(job_id, reservedUsd=reserve, knownCostUsd=known)
        try:
            if self.council_factory:
                runner = self.council_factory(FrozenStore(self.store, cfg), self.vault)
            else:
                from .council import CouncilRunner
                from .providers import ProviderRouter
                runner = CouncilRunner(FrozenStore(self.store, cfg), self.vault, cli=self.cli,
                                       router=ProviderRouter(self.vault, cfg, catalog=self.catalog))
            result = await runner.run(**body, on_event=record)
            status = result.get("status", "failed")
            if status not in {"completed", "partial", "awaiting_host_synthesis", "failed"}:
                status = "failed"
            self.store.update_job(job_id, status=status, result=result, costUsd=result.get("costUsd"), reservedUsd=result.get("reservedUsd", 0))
            self.store.event(job_id, status, {"costUsd": result.get("costUsd"), "reservedUsd": result.get("reservedUsd")})
        except asyncio.CancelledError as exc:
            partial = exc.public() if callable(getattr(exc, "public", None)) else getattr(runner, "partial_result", None)
            current = self.store.job(job_id)
            self.store.update_job(job_id, status="cancelled", result=partial,
                                  costUsd=partial.get("costUsd") if isinstance(partial, dict) else None,
                                  knownCostUsd=partial.get("knownCostUsd", current.get("knownCostUsd", 0)) if isinstance(partial, dict) else current.get("knownCostUsd", 0),
                                  reservedUsd=partial.get("reservedUsd", current.get("reservedUsd", 0)) if isinstance(partial, dict) else current.get("reservedUsd", 0),
                                  error="Cancelled; accrued evidence and possible provider charges are retained. Check usage before retry.")
            self.store.event(job_id, "cancelled", {"partialResult": partial, "reservedUsd": self.store.job(job_id).get("reservedUsd")})
            raise
        except Exception as exc:
            error = exc.public() if callable(getattr(exc, "public", None)) else safe_error(exc)
            self.store.update_job(job_id, status="failed", error=error, costUsd=error.get("costUsd"), reservedUsd=error.get("reservedUsd", 0))
            self.store.event(job_id, "failed", error)

    async def job(self, request):
        job = self.store.job(request.path_params["job_id"])
        if not job:
            return JSONResponse({"error": "Unknown job"}, status_code=404)
        return JSONResponse(sanitize(job))

    async def cancel(self, request):
        jid = request.path_params["job_id"]
        if jid in self.tasks:
            self.tasks[jid].cancel()
            return JSONResponse({"id": jid, "status": "cancellation_requested"})
        return JSONResponse(sanitize(await self.cli.cancel(jid)))

    async def jev(self, request):
        from .jev import JevTools
        body = await self.body(request)
        if not isinstance(body, dict):
            raise ValueError("Invalid Jev request")
        method = body.pop("operation", "check")
        if method not in {"check", "rank"}:
            raise ValueError("Unsupported Jev operation")
        allowed = {"state", "question", "threshold"}
        if method == "rank":
            allowed |= {"candidates", "instructions"}
        if set(body) - allowed or not isinstance(body.get("state"), str) or not body["state"].strip():
            raise ValueError("Invalid Jev request")
        arguments = dict(body)
        if method == "rank":
            if not isinstance(body.get("candidates"), list) or ("question" in body and "instructions" in body):
                raise ValueError("Invalid Jev ranking request")
            # MCP exposes one optional question; the typed ranker calls it
            # instructions. Preserve direct HTTP instructions/threshold callers.
            question = arguments.pop("question", None)
            if question is not None:
                arguments["instructions"] = question
            if "instructions" in arguments and (not isinstance(arguments["instructions"], str) or not arguments["instructions"].strip()):
                raise ValueError("Invalid Jev ranking instructions")
        elif not isinstance(body.get("question"), str) or not body["question"].strip():
            raise ValueError("Invalid Jev check question")
        cfg = self.store.config()
        if not cfg["jev"]["enabled"] or not cfg["jev"]["providerId"]:
            return JSONResponse({"error": "Jev is not configured. Add its key and enable it in Connections."}, status_code=409)
        job = self.store.create_job("jev", {"operation": method, **body, "configRevision": cfg["revision"]})
        self.store.update_job(job["id"], status="running", pid=os.getpid())
        try:
            tool = JevTools(self.vault, cfg)
            result = sanitize(await getattr(tool, method)(**arguments))
            self.store.update_job(job["id"], status="completed", result=result, costUsd=result.get("costUsd"), reservedUsd=result.get("reservedUsd", 0))
            return JSONResponse({**result, "jobId": job["id"]})
        except Exception as exc:
            error = exc.public() if callable(getattr(exc, "public", None)) else safe_error(exc)
            self.store.update_job(job["id"], status="failed", error=error, result=error.get("partial"), costUsd=error.get("costUsd"), reservedUsd=error.get("reservedUsd", 0))
            return JSONResponse({"error": sanitize(error), "jobId": job["id"]}, status_code=502)

    def app(self, token):
        def guarded(fn):
            async def handler(request):
                try:
                    return await fn(request)
                except ConflictError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=409)
                except (ValueError, TypeError, json.JSONDecodeError):
                    return JSONResponse({"error": "Invalid request; check settings and required fields"}, status_code=400)
                except KeyError:
                    return JSONResponse({"error": "Unknown configured resource"}, status_code=404)
                except Exception as exc:
                    # Do not forward raw provider/helper exceptions, headers or secret-bearing bodies.
                    # These internal catalog codes give the native client an
                    # actionable cause without exposing an upstream message,
                    # partial result, credential or local authorization token.
                    from .providers import RoutingError
                    if isinstance(exc, RoutingError) and (
                        exc.code in {"catalog_unavailable", "catalog_connection_error", "catalog_timeout",
                                     "missing_key", "unsupported_protocol", "invalid_endpoint",
                                     "private_endpoint_requires_confirmation", "http_requires_loopback"}
                        or re.fullmatch(r"catalog_http_[0-9]{3}", exc.code or "")
                    ):
                        message = ("Catalog connection failed; check network, VPN, and saved endpoint."
                                   if exc.code in {"catalog_connection_error", "catalog_timeout"}
                                   else "Provider catalog could not be loaded.")
                        return JSONResponse({"error": exc.code, "message": message}, status_code=502)
                    return JSONResponse({"error": type(exc).__name__, "message": "Operation failed; inspect the sanitized task status"}, status_code=502)
            return handler
        routes = [
            Route("/state", guarded(self.state)),
            Route("/config", guarded(self.config), methods=["PUT"]),
            Route("/providers/{provider_id}/key", guarded(self.key), methods=["POST", "DELETE"]),
            Route("/providers/{provider_id}/models", guarded(self.models), methods=["POST"]),
            Route("/agents", guarded(self.agents)),
            Route("/install/{client}", guarded(self.install), methods=["POST"]),
            Route("/uninstall/{client}", guarded(self.uninstall), methods=["POST"]),
            Route("/jobs", guarded(self.jobs), methods=["POST"]),
            Route("/jobs/{job_id}", guarded(self.job)),
            Route("/jobs/{job_id}/cancel", guarded(self.cancel), methods=["POST"]),
            Route("/jev", guarded(self.jev), methods=["POST"]),
        ]
        app = Starlette(routes=routes)
        app.add_middleware(AuthMiddleware, token=token)
        return app


def serve(state_dir, port=0):
    from .build_info import get_build_info
    store = Store(state_dir)
    lock = FileLock(store.state_dir / "backend.lock")
    try:
        lock.acquire(timeout=0)
    except LockTimeout:
        # Native and MCP bootstrap share the already-running authority. Wait
        # only for its startup descriptor; never create another token/server.
        from .mcp_server import LocalBackend
        reader = LocalBackend(store.state_dir)
        for _ in range(100):
            data = reader.descriptor()
            if data:
                print(json.dumps({"port": data["port"], "token": data["token"]}), flush=True)
                return
            time.sleep(0.1)
        raise RuntimeError("An owned backend is starting; no duplicate was launched")
    token = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    chosen = sock.getsockname()[1]
    descriptor = store.state_dir / "runtime.json"
    temporary = descriptor.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"port": chosen, "token": token, "pid": os.getpid(), "build": get_build_info()}, out)
    os.replace(temporary, descriptor)
    print(json.dumps({"port": chosen, "token": token}), flush=True)
    server = uvicorn.Server(uvicorn.Config(Service(store, Vault(store.state_dir)).app(token), host="127.0.0.1", port=chosen, access_log=False, log_level="warning"))
    try:
        server.run(sockets=[sock])
    finally:
        lock.release()
