#!/usr/bin/env python3
"""Generate public build receipt and package license inventory before freezing."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pair_core.build_info import source_manifest


def licenses(destination):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    roots = ["mcp", "openai", "anthropic", "httpx", "pydantic", "uvicorn", "filelock", "pyinstaller"]
    found, pending = {}, list(roots)
    while pending:
        name = canonicalize_name(pending.pop())
        if name in found:
            continue
        distribution = metadata.distribution(name)
        record = {"name": name, "version": distribution.version,
                  "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "unspecified",
                  "files": []}
        for file in distribution.files or []:
            if file.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                source = distribution.locate_file(file)
                if source.is_file():
                    target = destination / name / str(file).replace("/", "__")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    record["files"].append(target.relative_to(destination).as_posix())
        found[name] = record
        for raw in distribution.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    interpreter_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if interpreter_license.is_file():
        shutil.copyfile(interpreter_license, destination / "PYTHON-LICENSE.txt")
    records = sorted(found.values(), key=lambda row: row["name"])
    (destination / "DEPENDENCIES.json").write_text(json.dumps(records, indent=2) + "\n")
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "release-info.json")
    parser.add_argument("--legal-output", type=Path)
    args = parser.parse_args()
    receipt = source_manifest(ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    if args.legal_output:
        args.legal_output.mkdir(parents=True, exist_ok=True)
        licenses(args.legal_output)
    print(json.dumps({"buildId": receipt["buildId"], "sourceFingerprint": receipt["sourceFingerprint"], "inputCount": len(receipt["inputs"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
