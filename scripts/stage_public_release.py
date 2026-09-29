#!/usr/bin/env python3
"""Stage a source-only GitHub ZIP without publishing Git history or private state.

The default release gate requires committed public inputs. --allow-working-tree
is for local preparation only; its output must not be published as a release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pair_core.build_info import source_manifest, source_paths  # noqa: E402

PUBLIC_DOCS = (".gitignore", "README.md", "QUICKSTART.md", "INTERFACES.md")
PRIVATE_PARTS = {".git", ".private", ".venv", "build", "dist", "__pycache__", "node_modules"}
LOCAL_LINK = re.compile(r"\]\(([^)]+)\)")


class StageError(ValueError):
    pass


def public_paths(root: Path) -> list[Path]:
    paths = set(source_paths(root))
    paths.update(root / name for name in PUBLIC_DOCS)
    tests = root / "tests"
    if tests.is_symlink() or not tests.is_dir():
        raise StageError("Missing or linked tests directory")
    paths.update(tests.rglob("*.py"))
    selected = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if (not path.is_file() or path.is_symlink() or
                any(part in PRIVATE_PARTS or part.startswith(".env") for part in relative.parts)):
            raise StageError("Unsafe public input: " + relative.as_posix())
        selected.append(path)
    return selected


def check_public_content(root: Path, paths: list[Path]) -> None:
    selected = {path.relative_to(root).as_posix() for path in paths}
    home = Path.home().as_posix().encode()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        if home in payload:
            raise StageError("Personal home path in public input: " + relative)
        if path.suffix != ".md":
            continue
        for raw in LOCAL_LINK.findall(payload.decode("utf-8")):
            target = raw.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            if target.startswith("/"):
                raise StageError("Absolute Markdown link in public input: " + relative)
            resolved = os.path.normpath(str(Path(relative).parent / target)).replace("\\", "/")
            if resolved not in selected:
                raise StageError("Broken public Markdown link: " + relative + " -> " + target)


def check_committed(root: Path, paths: list[Path]) -> None:
    selected = {path.relative_to(root).as_posix() for path in paths}
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    tracked_names = {name.decode() for name in tracked.split(b"\0") if name}
    missing = sorted(selected - tracked_names)
    if missing:
        raise StageError("Public inputs are not tracked: " + ", ".join(missing))
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", *sorted(selected)],
                             cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
    if changed:
        raise StageError("Commit public inputs before release: " + ", ".join(changed))


def scan_export(directory: Path) -> None:
    if shutil.which("gitleaks") is None:
        raise StageError("gitleaks is required for the public source gate")
    result = subprocess.run(["gitleaks", "dir", "--no-banner", "--redact", "--exit-code", "1", str(directory)],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise StageError("Public source secret scan failed (exit " + str(result.returncode) + ")")


def write_zip(directory: Path, destination: Path) -> dict[str, str]:
    digest_by_name = {}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            name = path.relative_to(directory.parent).as_posix()
            payload = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            digest_by_name[name] = hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(destination) as archive:
        if set(archive.namelist()) != set(digest_by_name):
            raise StageError("Source ZIP file list changed during verification")
        for name, expected in digest_by_name.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise StageError("Source ZIP hash verification failed: " + name)
    return digest_by_name


def stage(output_dir: Path, allow_working_tree: bool = False) -> dict:
    paths = public_paths(ROOT)
    check_public_content(ROOT, paths)
    if not allow_working_tree:
        check_committed(ROOT, paths)
    output_dir = output_dir.resolve()
    if output_dir == ROOT or output_dir == Path.home() or output_dir == Path("/"):
        raise StageError("Choose a dedicated empty staging directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise StageError("Staging directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    prefix = "Pair-" + version + "-source"
    with tempfile.TemporaryDirectory(prefix="public-stage-", dir=output_dir) as temporary:
        source = Path(temporary) / prefix
        for path in paths:
            target = source / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        original = source_manifest(ROOT)["sourceFingerprint"]
        exported = source_manifest(source)["sourceFingerprint"]
        if original != exported:
            raise StageError("Exported source fingerprint differs from release inputs")
        scan_export(source)
        suffix = "-PREVIEW" if allow_working_tree else ""
        provisional = Path(temporary) / (prefix + suffix + ".zip")
        contents = write_zip(source, provisional)
        digest = hashlib.sha256(provisional.read_bytes()).hexdigest()
        archive = output_dir / provisional.name
        os.replace(provisional, archive)
    report = {"version": version, "sourceFingerprint": exported, "fileCount": len(contents),
              "archive": str(archive), "sha256": digest, "secretScan": "passed",
              "gitHistoryIncluded": False, "provisional": allow_working_tree,
              "publication": "not performed"}
    (output_dir / "stage-report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--allow-working-tree", action="store_true", help="Preview only; not publishable")
    args = parser.parse_args()
    try:
        print(json.dumps(stage(args.output_dir, args.allow_working_tree), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Public stage blocked: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
