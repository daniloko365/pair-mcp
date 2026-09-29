"""Public source export must omit owner evidence and preserve build inputs."""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

from pair_core.build_info import source_paths

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/stage_public_release.py"
SPEC = importlib.util.spec_from_file_location("stage_public_release", SCRIPT)
assert SPEC and SPEC.loader
public = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public)


def test_public_source_selection_excludes_internal_evidence():
    paths = public.public_paths(public.ROOT)
    names = {path.relative_to(public.ROOT).as_posix() for path in paths}
    assert {path.relative_to(public.ROOT).as_posix() for path in source_paths(public.ROOT)} <= names
    assert "README.md" in names
    assert "QUICKSTART.md" in names
    assert "03_STATE.md" not in names
    assert "SPEC.md" not in names
    assert "GITHUB_RELEASE_READINESS.md" not in names
    assert not any(name.startswith(".private/") or name.startswith("build/") for name in names)
    public.check_public_content(public.ROOT, paths)


def test_public_source_zip_contains_no_git_history_or_owner_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(public, "scan_export", lambda _directory: None)
    result = public.stage(tmp_path, allow_working_tree=True)
    assert result["gitHistoryIncluded"] is False
    assert result["provisional"] is True
    with zipfile.ZipFile(result["archive"]) as archive:
        names = archive.namelist()
    assert any(name.endswith("/pair_core/mcp_server.py") for name in names)
    assert not any("/.git/" in name or name.endswith("/03_STATE.md") for name in names)
    assert not any(name.endswith("/SPEC.md") or name.endswith("/GITHUB_RELEASE_READINESS.md") for name in names)


def test_public_content_rejects_missing_link_and_personal_home(tmp_path):
    page = tmp_path / "README.md"
    page.write_text("[private](03_STATE.md)\n")
    with pytest.raises(public.StageError, match="Broken public Markdown link"):
        public.check_public_content(tmp_path, [page])
    page.write_text(Path.home().as_posix() + "\n")
    with pytest.raises(public.StageError, match="Personal home path"):
        public.check_public_content(tmp_path, [page])
