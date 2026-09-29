"""Scoped client integration using official CLIs; no host provider replacement."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .mcp_server import clean_environment
from .security import sanitize


def product_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=clean_environment(), check=False)
    if result.returncode:
        raise RuntimeError(f"Client integration failed (exit {result.returncode}); existing private client settings were not printed")
    return result.stdout


def tree_sha(path):
    if Path(path).is_symlink():
        raise ValueError("Pair skill target is a symlink; leave it unchanged")
    digest = hashlib.sha256()
    allowed = {"SKILL.md", "agents/openai.yaml"}
    for item in sorted(Path(path).rglob("*")):
        if item.is_symlink():
            raise ValueError("Pair skill target contains an unexpected symlink; leave it unchanged")
        if item.is_file():
            if item.relative_to(path).as_posix() not in allowed:
                raise ValueError("Pair skill has owner-added files; leave them unread and unchanged")
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _server_digest(client, executable):
    """Hash only this integration's opaque transport; never print env values."""
    command = [executable, "mcp", "get", "pair"] + (["--json"] if client == "codex" else [])
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, env=clean_environment())
    if result.returncode:
        message = (result.stderr + result.stdout).strip().lower()
        if "no mcp server" in message and "pair" in message and result.returncode == 1:
            return None
        raise RuntimeError("Cannot verify Pair client entry; private settings were not printed and no removal was attempted")
    if client == "codex":
        server = json.loads(result.stdout)
        value = server.get("transport", server)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        # Native CLI health/status changes are not transport edits.
        raw = "\n".join(line.strip() for line in result.stdout.splitlines()
                        if not line.strip().startswith(("Status:", "Name:", "Scope:")) and line.strip())
    return hashlib.sha256(raw.encode()).hexdigest()


def install(client, state_dir):
    from .cli import discover_executable
    executable = discover_executable(client)
    if not executable:
        raise ValueError(f"Official {client} CLI is not installed; install its official client first")
    state = Path(state_dir)
    owned = state / "integration"
    owned.mkdir(mode=0o700, parents=True, exist_ok=True)
    skill_source = product_root() / "plugins" / "pair-companion" / "skills" / "pair-work"
    if not (skill_source / "SKILL.md").is_file():
        raise RuntimeError("Bundled Pair skill is missing")
    skill_parent = Path.home() / (".codex" if client == "codex" else ".claude") / "skills"
    target = skill_parent / "pair-work"
    marker = owned / (client + ".json")
    if target.is_symlink() or marker.is_symlink():
        raise ValueError("Pair integration target is a symlink; leave it unchanged")
    if target.exists() and not marker.exists():
        raise ValueError("An existing unrelated pair-work skill would be overwritten; leave it unchanged")
    previous = json.loads(marker.read_text()) if marker.exists() else None
    current_digest = _server_digest(client, executable)
    if previous and previous.get("serverDigest"):
        if current_digest and current_digest != previous["serverDigest"]:
            raise ValueError("Pair MCP transport was edited; leave it unchanged")
    elif current_digest and not previous:
        raise ValueError("A pre-existing Pair MCP entry was not replaced")
    if previous and target.exists():
        expected = previous.get("treeSha256")
        if not expected or tree_sha(target) != expected:
            raise ValueError("Pair skill was edited after installation; preserve the edits before updating")
    # Opaque backup: values are neither parsed nor printed. Only Pair CLI commands mutate settings.
    config = Path.home() / ".codex" / "config.toml" if client == "codex" else Path.home() / ".claude.json"
    if config.is_file() and not marker.exists():
        backup = owned / (client + "-config-before")
        if not backup.exists():
            shutil.copyfile(config, backup)
            os.chmod(backup, 0o600)
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "pair_core"]
    if client == "codex":
        from .vault import Vault
        # Refuse a foreign server occupying this name; do not print its config/env.
        probe = subprocess.run([executable, "mcp", "get", "pair", "--json"], capture_output=True, text=True, timeout=15, env=clean_environment())
        if probe.returncode == 0:
            server = json.loads(probe.stdout)
            transport = server.get("transport", server)
            if not previous or transport.get("command") != previous.get("backend"):
                raise ValueError("A pre-existing or edited Pair MCP entry was not replaced")
        command = [executable, "mcp", "add", "pair", "--env", "PAIR_KEYCHAIN_HELPER=" + str(Vault(state).helper), "--", *prefix, "mcp", "--state", str(state)]
    else:
        from .vault import Vault
        probe = subprocess.run([executable, "mcp", "get", "pair"], capture_output=True, text=True, timeout=15, env=clean_environment())
        if probe.returncode == 0:
            if not previous:
                raise ValueError("A pre-existing Claude Pair MCP entry was not replaced")
            command_line = next((line.split(":", 1)[1].strip() for line in probe.stdout.splitlines() if line.strip().startswith("Command:")), None)
            if command_line != previous.get("backend"):
                raise ValueError("Claude Pair MCP entry was edited; leave it unchanged")
        command = [executable, "mcp", "add", "--scope", "user", "--env", "PAIR_KEYCHAIN_HELPER=" + str(Vault(state).helper), "pair", "--", *prefix, "mcp", "--state", str(state)]
    _run(command)
    try:
        server_digest = _server_digest(client, executable)
        if server_digest is None:
            raise RuntimeError("Official client did not retain the Pair entry")
        skill_parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_source, target, dirs_exist_ok=marker.exists())
        info = {"client": client, "server": "pair", "skill": str(target), "installedAt": time.time(), "sourceSha256": hashlib.sha256((skill_source / "SKILL.md").read_bytes()).hexdigest(), "treeSha256": tree_sha(target), "backend": sys.executable, "serverDigest": server_digest}
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(info, out)
    except Exception:
        if previous is None:
            # Undo only the newly added Pair entry, never restore the whole
            # host config or remove a foreign provider/integration.
            remove = [executable, "mcp", "remove", "pair"] if client == "codex" else [executable, "mcp", "remove", "--scope", "user", "pair"]
            try:
                _run(remove)
            except Exception:
                pending = owned / (client + "-pending-recovery.json")
                fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as out:
                    json.dump({"client": client, "ownedServer": "pair", "backend": sys.executable}, out)
            recovery = owned / (client + "-incomplete-" + str(time.time_ns()))
            recovery.mkdir(mode=0o700)
            if target.exists() and not target.is_symlink():
                shutil.move(str(target), str(recovery / "pair-work"))
            if marker.exists() and not marker.is_symlink():
                shutil.move(str(marker), str(recovery / "receipt.json"))
        raise RuntimeError("Pair integration did not finish; the newly added entry was rolled back or recorded for scoped recovery") from None
    return {"connected": True, "client": client, "server": "pair", "skill": "pair-work", "newChatRequired": True, "mainProviderChanged": False}


def uninstall(client, state_dir):
    """Remove only unmodified Pair-owned entries. Keep all keys/jobs/settings."""
    if client not in {"codex", "claude"}:
        raise ValueError("Unsupported client")
    from .cli import discover_executable
    executable = discover_executable(client)
    if not executable:
        raise ValueError("Official client CLI is unavailable")
    marker = Path(state_dir) / "integration" / (client + ".json")
    target = Path.home() / (".codex" if client == "codex" else ".claude") / "skills" / "pair-work"
    if marker.is_symlink() or target.is_symlink():
        raise ValueError("Pair integration target is a symlink; leave it unchanged")
    if not marker.exists():
        return {"client": client, "removed": False, "message": "No owned integration receipt; nothing changed"}
    receipt = json.loads(marker.read_text())
    if receipt.get("client") != client or receipt.get("skill") != str(target):
        raise ValueError("Invalid owned integration receipt")
    if target.exists() and tree_sha(target) != receipt.get("treeSha256"):
        raise ValueError("Pair skill has owner edits; preserve it and leave integration unchanged")
    current = _server_digest(client, executable)
    if current is not None:
        if not receipt.get("serverDigest") or current != receipt["serverDigest"]:
            raise ValueError("Pair MCP transport is foreign or edited; leave it unchanged")
        command = [executable, "mcp", "remove", "pair"] if client == "codex" else [executable, "mcp", "remove", "--scope", "user", "pair"]
        _run(command)
    # Recoverable scoped removal, not restoring stale whole-client config.
    archived = marker.parent / (client + "-removed-" + str(time.time_ns()))
    archived.mkdir(mode=0o700)
    if target.exists():
        shutil.move(str(target), str(archived / "pair-work"))
    shutil.move(str(marker), str(archived / "receipt.json"))
    return {"client": client, "removed": True, "archive": str(archived), "keysAndJobsPreserved": True, "mainProviderChanged": False}
