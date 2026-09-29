#!/usr/bin/env python3
"""Falsifiable standalone Mac artifact checks. Never starts a provider turn."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pair_core.build_info import source_manifest, validate_receipt

BINARIES = ("Contents/MacOS/Pair", "Contents/MacOS/PairCore", "Contents/MacOS/pair-keychain", "Contents/Resources/Core/PairCore")
MINIMUM_OS_BINARIES = BINARIES + ("Contents/Resources/Core/_internal/libpython3.13.dylib",)
PRIVATE_NAMES = {".env", ".private", "pair.sqlite3", "pair.db", "state.sqlite3", "service.json", "runtime.json", "auth.json", "credentials.json"}


def run(command, timeout=30):
    environment = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}}
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=environment, check=False)
    if result.returncode:
        raise ValueError("Release check command failed: " + Path(command[0]).name + " (exit " + str(result.returncode) + ")")
    return result.stdout


def _os_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value):
        raise ValueError("Invalid macOS version metadata")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (3 - len(parts))


def macos_minimum_versions(output):
    """Read load-command minimum, never SDK or linker-tool version."""
    minimums = []
    for block in re.split(r"(?=Load command [0-9]+)", output):
        if re.search(r"^\s*cmd\s+LC_BUILD_VERSION\s*$", block, re.MULTILINE):
            if not re.search(r"^\s*platform\s+MACOS\s*$", block, re.MULTILINE):
                raise ValueError("Load command is not macOS")
            match = re.search(r"^\s*minos\s+([^\s]+)\s*$", block, re.MULTILINE)
        elif re.search(r"^\s*cmd\s+LC_VERSION_MIN_MACOSX\s*$", block, re.MULTILINE):
            match = re.search(r"^\s*version\s+([^\s]+)\s*$", block, re.MULTILINE)
        else:
            continue
        if not match:
            raise ValueError("Missing macOS minimum version")
        minimums.append(_os_version(match.group(1)))
    if not minimums:
        raise ValueError("Missing macOS minimum version load command")
    return minimums


def verify_minimum_macos(bundle, declared):
    target = _os_version(declared)
    minimums = {}
    for name in MINIMUM_OS_BINARIES:
        versions = macos_minimum_versions(run(["/usr/bin/xcrun", "vtool", "-show-build", str(Path(bundle) / name)]))
        if any(version > target for version in versions):
            raise ValueError("Binary minimum macOS is higher than Info.plist declaration")
        minimums[name] = [".".join(map(str, version)) for version in versions]
    return minimums


def check_bundle(bundle, source_root, expected=None):
    bundle = Path(bundle)
    checks, errors, identities = {}, [], {}

    def check(name, callback):
        try:
            checks[name] = bool(callback())
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            checks[name] = False
        if not checks[name]:
            errors.append(name + "Failed")

    def receipt_check():
        receipt = validate_receipt(json.loads((bundle / "Contents/Resources/Core/_internal/release-info.json").read_text()))
        identities["receipt"] = {key: receipt[key] for key in ("version", "buildId", "sourceFingerprint", "architecture", "builtAt")}
        return receipt["sourceFingerprint"] == expected if expected else True

    check("receipt", receipt_check)
    check("signature", lambda: run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)]) == "")
    def architecture():
        for name in BINARIES:
            path = bundle / name
            summary = run(["/usr/bin/file", "-b", str(path)]).strip()
            if "Mach-O" not in summary or platform.machine() not in summary:
                return False
            identities.setdefault("binaries", {})[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "architecture": summary}
        return True
    check("nativeArchitecture", architecture)
    def runtime_probe():
        value = json.loads(run([str(bundle / "Contents/MacOS/PairCore"), "check-release"], timeout=45))
        identities["runtime"] = value
        return value.get("ok") is True and value.get("networkCalls") == 0 and value.get("secretsRead") is False
    check("frozenRuntime", runtime_probe)
    check("receiptMatchesRuntime", lambda: identities["receipt"]["sourceFingerprint"] == identities["runtime"]["build"]["sourceFingerprint"])
    if source_root:
        fresh = source_manifest(Path(source_root))
        identities["currentSource"] = {"buildId": fresh["buildId"], "sourceFingerprint": fresh["sourceFingerprint"]}
        check("sourceMatchesFrozenArtifact", lambda: fresh["sourceFingerprint"] == identities["runtime"]["build"]["sourceFingerprint"])
    def native_version():
        with (bundle / "Contents/Info.plist").open("rb") as file:
            plist = plistlib.load(file)
        return plist["CFBundleIdentifier"] == "dev.pair-companion.app" and plist["CFBundleShortVersionString"] == identities["receipt"]["version"]
    check("nativeVersion", native_version)
    def minimum_macos():
        with (bundle / "Contents/Info.plist").open("rb") as file:
            declared = plistlib.load(file)["LSMinimumSystemVersion"]
        identities["minimumMacOS"] = {"declared": declared, "binaries": verify_minimum_macos(bundle, declared),
                                       "physicalOldOSAcceptance": "not verified"}
        return True
    check("minimumMacOS", minimum_macos)
    def legal():
        directory = bundle / "Contents/Resources/Legal"
        required = ("PAIR-LICENSE", "PAL-LICENSE", "PAL-NOTICE", "THIRD_PARTY_PAL.md", "DEPENDENCIES.json", "PYTHON-LICENSE.txt")
        if not all((directory / name).is_file() for name in required):
            return False
        records = json.loads((directory / "DEPENDENCIES.json").read_text())
        return all(record.get("files") and all((directory / file).is_file() for file in record["files"]) for record in records)
    check("licensesAndNotices", legal)
    def private_files():
        return not any(path.name in PRIVATE_NAMES or path.name.startswith(".env.") or ".private" in path.parts for path in bundle.rglob("*"))
    check("noPrivateStateOrCredentials", private_files)
    def pal_provenance():
        root = bundle / "Contents/Resources/Core/_internal/vendor/pal"
        provenance = json.loads((root / "PROVENANCE.json").read_text())
        return provenance["revision"] == "7afc7c1cc96e23992c8f105f960132c657883bb1" and provenance["license"] == "Apache-2.0" and (root / "LICENSE").is_file() and (root / "NOTICE").is_file()
    check("pinnedPalProvenance", pal_provenance)
    return {"ok": not errors, "checks": checks, "errors": errors, "identities": identities,
            "networkCalls": 0, "notarized": False, "endToEndAcceptance": "separate live checks required"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/Pair.app")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    result = check_bundle(args.bundle, args.source_root, args.expected_fingerprint)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
