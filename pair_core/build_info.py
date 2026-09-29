"""Public build identity and credential-free runtime probes.

A semantic version alone cannot identify the backend: the frozen build embeds
the hash of its exact public inputs. A source checkout computes a separate live
identity. Neither path reads application state, environment or credentials.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SOURCE_DIRECTORIES = ("pair_core", "native", "plugins", "vendor/pal", "scripts")
ROOT_INPUTS = ("pyproject.toml", "uv.lock", "LICENSE", "THIRD_PARTY_PAL.md")
SOURCE_SUFFIXES = {".py", ".swift", ".sh", ".c", ".yaml", ".yml", ".md", ".plist", ".spec"}
JSON_INPUTS = {"vendor/pal/PROVENANCE.json", "plugins/pair-companion/.codex-plugin/plugin.json",
               "plugins/pair-companion/.mcp.json", "plugins/pair-companion/plugin.json"}
EXCLUDED_DIRECTORIES = {".private", "node_modules", "build", "dist", "target", ".venv", "__pycache__", ".git"}
DIGEST = re.compile(r"^[a-f0-9]{64}$")
RECEIPT_NAME = "release-info.json"
_FROZEN_BUILD_INFO = None


def source_paths(root: Path) -> list[Path]:
    """Explicit runtime/native/plugin/vendor inputs, never broad root scanning."""
    root = Path(root)
    found = []
    for name in ROOT_INPUTS:
        item = root / name
        if item.is_symlink():
            raise ValueError("Release input is a symlink")
        if item.is_file():
            found.append(item)
    for name in SOURCE_DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            raise ValueError("Release input directory is a symlink")
        if not directory.is_dir():
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            for child in list(directories):
                if child in EXCLUDED_DIRECTORIES:
                    directories.remove(child)
                elif (Path(current) / child).is_symlink():
                    raise ValueError("Release input directory contains a symlink")
            for name in files:
                # Do not inspect even a placeholder credential/state file.
                if name.startswith(".env") or name.endswith(".log"):
                    continue
                item = Path(current) / name
                relative = item.relative_to(root).as_posix()
                if item.suffix in SOURCE_SUFFIXES or name in {"LICENSE", "NOTICE"} or relative in JSON_INPUTS:
                    if item.is_symlink():
                        raise ValueError("Release input contains a symlink")
                    found.append(item)
    return sorted(set(found), key=lambda item: item.relative_to(root).as_posix())


def _fingerprint(version: str, inputs: dict[str, str]) -> str:
    payload = {"schemaVersion": 1, "version": version, "inputs": inputs}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_manifest(root: Path) -> dict:
    root = Path(root)
    paths = source_paths(root)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    inputs = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    fingerprint = _fingerprint(version, inputs)
    return {"schemaVersion": 1, "product": "Pair", "version": version,
            "buildId": version + "-" + fingerprint[:12], "sourceFingerprint": fingerprint,
            "architecture": platform.machine(), "builtAt": datetime.now(timezone.utc).isoformat(), "inputs": inputs}


def validate_receipt(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("product") != "Pair":
        raise ValueError("Unsupported Pair release receipt")
    version, inputs = value.get("version"), value.get("inputs")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid release version")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError("Release receipt has no public inputs")
    for name, digest in inputs.items():
        if not isinstance(name, str) or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name:
            raise ValueError("Invalid release input path")
        if any(part in EXCLUDED_DIRECTORIES or part.startswith(".env") for part in PurePosixPath(name).parts):
            raise ValueError("Private path in release receipt")
        if name not in ROOT_INPUTS and not any(name.startswith(directory + "/") for directory in SOURCE_DIRECTORIES):
            raise ValueError("Path outside public release inputs")
        if PurePosixPath(name).suffix == ".json" and name not in JSON_INPUTS:
            raise ValueError("Nonpublic JSON path in release receipt")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError("Invalid release input digest")
    fingerprint = _fingerprint(version, inputs)
    if value.get("sourceFingerprint") != fingerprint or value.get("buildId") != version + "-" + fingerprint[:12]:
        raise ValueError("Release receipt fingerprint does not match its public inputs")
    return value


def get_build_info() -> dict:
    global _FROZEN_BUILD_INFO
    import copy
    from . import __version__
    frozen = bool(getattr(sys, "frozen", False))
    if frozen and _FROZEN_BUILD_INFO is not None:
        return copy.deepcopy(_FROZEN_BUILD_INFO)
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    info = {"version": __version__, "mode": "frozen" if frozen else "source", "receiptPresent": False,
            "sourceFingerprint": None, "buildId": None, "runtimeArchitecture": platform.machine()}
    try:
        receipt = validate_receipt(json.loads((root / RECEIPT_NAME).read_text())) if frozen else source_manifest(root)
        if receipt["version"] != __version__:
            if frozen:
                _FROZEN_BUILD_INFO = copy.deepcopy(info)
            return info
    except (OSError, ValueError, KeyError, TypeError):
        if frozen:
            _FROZEN_BUILD_INFO = copy.deepcopy(info)
        return info
    result = {**info, "version": receipt["version"], "receiptPresent": frozen,
            "sourceFingerprint": receipt["sourceFingerprint"], "buildId": receipt["buildId"],
            "buildArchitecture": receipt["architecture"], "inputCount": len(receipt["inputs"]), "builtAt": receipt["builtAt"] if frozen else None}
    if frozen:
        _FROZEN_BUILD_INFO = copy.deepcopy(result)
    return result


def release_probe(*, expected_fingerprint: str | None = None) -> dict:
    """No network/state/keys: prove parser and typed SDK imports inside artifact."""
    checks, errors = {}, []

    def check(name, callback):
        try:
            checks[name] = bool(callback())
        except Exception:
            checks[name] = False
        if not checks[name]:
            errors.append(name + "Failed")

    def mcp_probe():
        from mcp_types import ListToolsResult
        from mcp.server import Server
        return ListToolsResult.model_validate({"tools": []}).tools == [] and Server is not None

    def openai_probe():
        from openai.types.chat import ChatCompletion
        result = ChatCompletion.model_validate({"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "release-probe"}, "finish_reason": "stop"}]})
        return result.choices[0].message.content == "release-probe"

    def anthropic_probe():
        from anthropic.types import Message
        result = Message.model_validate({"id": "fixture", "type": "message", "role": "assistant", "model": "fixture-model",
            "content": [{"type": "text", "text": "release-probe"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
        return result.content[0].text == "release-probe"

    def parser_probe(agent):
        from .pal_bridge import _load_vendor, REVISION
        _load_vendor()
        if REVISION != "7afc7c1cc96e23992c8f105f960132c657883bb1":
            return False
        if agent == "codex":
            from pair_core._pal_vendor.parsers.codex import CodexJSONLParser
            result = CodexJSONLParser().parse('{"type":"item.completed","item":{"type":"agent_message","text":"release-probe"}}\n', "")
        else:
            from pair_core._pal_vendor.parsers.claude import ClaudeJSONParser
            result = ClaudeJSONParser().parse('{"type":"result","result":"release-probe","is_error":false}', "")
        return result.content == "release-probe"

    check("mcpTypedSDK", mcp_probe)
    check("openaiTypedSDK", openai_probe)
    check("anthropicTypedSDK", anthropic_probe)
    check("codexParser", lambda: parser_probe("codex"))
    check("claudeParser", lambda: parser_probe("claude"))
    check("cliWorkerImport", lambda: callable(importlib.import_module("pair_core.cli_worker").main))
    build = get_build_info()
    if build["sourceFingerprint"] is None:
        errors.append("buildIdentityUnknown")
    if expected_fingerprint is not None and build["sourceFingerprint"] != expected_fingerprint:
        errors.append("sourceFingerprintMismatch")
    return {"ok": not errors, "build": build, "checks": checks, "errors": errors, "networkCalls": 0, "secretsRead": False}
