"""Release identity and frozen-parser checks; no provider or credential reads."""
from __future__ import annotations

import copy
import importlib.util
import json
import plistlib
import tomllib
from pathlib import Path

import pytest

from pair_core import build_info
from pair_core import __version__


@pytest.fixture(autouse=True)
def isolate_process_identity(monkeypatch):
    # Each simulated frozen root is a different process; real frozen identity
    # deliberately remains immutable for that process's lifetime.
    monkeypatch.setattr(build_info, "_FROZEN_BUILD_INFO", None)


def sample_source(root: Path):
    (root / "pair_core").mkdir()
    (root / "pair_core" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
    (root / "pair_core" / "service.py").write_text("RESULT = 1\n")
    (root / "pair_core" / "credentials.json").write_text('{"fixture":"not-a-real-secret"}')
    (root / "pyproject.toml").write_text(f'[project]\nname="pair-companion"\nversion="{__version__}"\n')
    (root / "uv.lock").write_text("version = 1\n")
    (root / ".private").mkdir()
    (root / ".private" / "fixture-state.json").write_text('{"private":"fixture"}')
    (root / "build").mkdir()
    (root / "build" / "receipt.json").write_text("{}")
    (root / ".env").write_text("PUBLIC_FIXTURE_ONLY=placeholder\n")


def test_public_version_declarations_match():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    with (root / "native/Info.plist").open("rb") as file:
        app = plistlib.load(file)
    plugin = json.loads((root / "plugins/pair-companion/.codex-plugin/plugin.json").read_text())
    assert __version__ == project["project"]["version"]
    assert app["CFBundleShortVersionString"] == __version__
    assert plugin["version"] == __version__


def test_source_identity_ignores_private_state_and_never_reads_credentials(tmp_path, monkeypatch):
    sample_source(tmp_path)
    real_read = Path.read_bytes

    def public_read(path):
        if path.name.startswith(".env") or path.name == "credentials.json" or ".private" in path.parts:
            raise AssertionError("Release input scanner crossed the credential boundary")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", public_read)
    before = build_info.source_manifest(tmp_path)
    assert set(before["inputs"]) == {"pair_core/__init__.py", "pair_core/service.py", "pyproject.toml", "uv.lock"}
    (tmp_path / "build" / "receipt.json").write_text('{"changed":true}')
    assert build_info.source_manifest(tmp_path)["sourceFingerprint"] == before["sourceFingerprint"]
    (tmp_path / "pair_core" / "service.py").write_text("RESULT = 2\n")
    assert build_info.source_manifest(tmp_path)["sourceFingerprint"] != before["sourceFingerprint"]


def test_runtime_input_symlinks_fail_closed(tmp_path):
    sample_source(tmp_path)
    (tmp_path / "pair_core" / "linked.py").symlink_to(tmp_path / ".private" / "fixture-state.json")
    with pytest.raises(ValueError, match="symlink"):
        build_info.source_manifest(tmp_path)


def test_pyproject_symlink_is_rejected_before_read(tmp_path):
    sample_source(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").symlink_to(tmp_path / ".env")
    with pytest.raises(ValueError, match="symlink"):
        build_info.source_manifest(tmp_path)


def test_receipt_fingerprint_is_verified_not_trusted(tmp_path):
    sample_source(tmp_path)
    receipt = build_info.source_manifest(tmp_path)
    assert build_info.validate_receipt(receipt)["sourceFingerprint"] == receipt["sourceFingerprint"]
    edited = copy.deepcopy(receipt)
    edited["inputs"]["pair_core/service.py"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        build_info.validate_receipt(edited)


def test_frozen_build_uses_embedded_identity_not_current_source(tmp_path, monkeypatch):
    sample_source(tmp_path)
    receipt = build_info.source_manifest(tmp_path)
    (tmp_path / "release-info.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(build_info.sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_info.sys, "_MEIPASS", str(tmp_path), raising=False)
    info = build_info.get_build_info()
    assert info["mode"] == "frozen"
    assert info["sourceFingerprint"] == receipt["sourceFingerprint"]
    (tmp_path / "pair_core" / "service.py").write_text("RESULT = 2\n")
    assert build_info.get_build_info()["sourceFingerprint"] == receipt["sourceFingerprint"]


def test_missing_frozen_receipt_is_unknown_not_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info.sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_info.sys, "_MEIPASS", str(tmp_path), raising=False)
    info = build_info.get_build_info()
    assert info["receiptPresent"] is False
    assert info["sourceFingerprint"] is None


def test_receipt_cannot_override_compiled_package_version(tmp_path, monkeypatch):
    sample_source(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="pair-companion"\nversion="9.9.9"\n')
    receipt = build_info.source_manifest(tmp_path)
    (tmp_path / "release-info.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(build_info.sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_info.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert build_info.get_build_info()["sourceFingerprint"] is None


def test_release_probe_exercises_real_typed_sdks_and_pinned_parsers():
    result = build_info.release_probe()
    assert result["ok"] is True
    assert all(result["checks"].values())
    assert set(result["checks"]) >= {"mcpTypedSDK", "openaiTypedSDK", "anthropicTypedSDK", "codexParser", "claudeParser", "cliWorkerImport"}


def test_wrong_expected_build_is_not_accepted():
    result = build_info.release_probe(expected_fingerprint="0" * 64)
    assert result["ok"] is False
    assert "sourceFingerprintMismatch" in result["errors"]


@pytest.fixture
def release_checker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check-release.py"
    spec = importlib.util.spec_from_file_location("pair_release_checker_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vtool_parser_accepts_modern_and_legacy_macos_commands(release_checker):
    modern = "Load command 10\n      cmd LC_BUILD_VERSION\n platform MACOS\n    minos 13.0\n      sdk 26.5\n     tool LD\n  version 1267.0\n"
    legacy = "Load command 4\n      cmd LC_VERSION_MIN_MACOSX\n  version 10.13\n      sdk 14.0\n"
    assert release_checker.macos_minimum_versions(modern + legacy) == [(13, 0, 0), (10, 13, 0)]
    with pytest.raises(ValueError, match="macOS"):
        release_checker.macos_minimum_versions(modern.replace("MACOS", "IOS"))


def test_minimum_os_checks_launcher_and_python_and_rejects_26_on_13(release_checker, monkeypatch, tmp_path):
    visited = []

    def fixture_vtool(command, timeout=30):
        assert command[:3] == ["/usr/bin/xcrun", "vtool", "-show-build"]
        visited.append(command[3])
        minimum = "26.0" if command[3].endswith("Contents/MacOS/PairCore") else "11.0"
        return "Load command 1\n cmd LC_BUILD_VERSION\n platform MACOS\n minos " + minimum + "\n sdk 26.5\n"

    monkeypatch.setattr(release_checker, "run", fixture_vtool)
    with pytest.raises(ValueError, match="higher"):
        release_checker.verify_minimum_macos(tmp_path, "13.0")
    monkeypatch.setattr(release_checker, "run", lambda command: "Load command 1\n cmd LC_BUILD_VERSION\n platform MACOS\n minos 11.0\n sdk 26.5\n")
    result = release_checker.verify_minimum_macos(tmp_path, "13.0")
    assert len(result) == 5
    assert "Contents/Resources/Core/_internal/libpython3.13.dylib" in result
