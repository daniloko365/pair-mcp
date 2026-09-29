import json
import os
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest

from pair_core.store import Store, ConflictError


def provider(pid="primary"):
    return {"id": pid, "name": "Primary", "protocol": "openai", "baseUrl": "https://example.com/v1"}


def test_defaults_have_no_hidden_limits(tmp_path):
    cfg = Store(tmp_path).config()
    assert cfg["limits"] == {"maxTokens": None, "timeSeconds": None, "budgetUsd": None}
    assert cfg["members"] == []


def test_save_roundtrip_and_stale_revision(tmp_path):
    store = Store(tmp_path)
    old = store.config()
    cfg = dict(old, providers=[provider()], members=[{"id": "m1", "label": "Reviewer", "routes": [{"providerId": "primary", "model": "exact/model", "effort": "high"}]}])
    saved = store.save_config(cfg)
    assert saved["revision"] == old["revision"] + 1
    assert Store(tmp_path).config() == saved
    with pytest.raises(ConflictError):
        store.save_config(cfg)


def test_keys_and_unknown_fields_cannot_enter_config(tmp_path):
    store = Store(tmp_path)
    cfg = dict(store.config(), providers=[dict(provider(), apiKey="fixture-private-value")])
    with pytest.raises(ValueError):
        store.save_config(cfg)
    assert "fixture-private-value" not in store.config().__repr__()


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=x", "file:///etc/passwd"])
def test_unsafe_endpoint_rejected(tmp_path, url):
    cfg = dict(Store(tmp_path).config(), providers=[dict(provider(), baseUrl=url)])
    with pytest.raises(ValueError):
        Store(tmp_path).save_config(cfg)


def test_fixture_loopback_requires_explicit_permission(tmp_path):
    store = Store(tmp_path)
    cfg = dict(store.config(), providers=[dict(provider(), baseUrl="http://127.0.0.1:19876/v1", allowLocal=True)])
    assert store.save_config(cfg)["providers"][0]["allowLocal"]


def test_missing_provider_and_duplicate_ids_rejected(tmp_path):
    store = Store(tmp_path)
    cfg = dict(store.config(), providers=[provider(), provider()])
    with pytest.raises(ValueError):
        store.save_config(cfg)
    cfg = dict(store.config(), members=[{"id": "m1", "label": "Unknown", "routes": [{"providerId": "absent", "model": "model"}]}])
    with pytest.raises(ValueError):
        store.save_config(cfg)


def test_jobs_survive_other_process_store_and_full_result(tmp_path):
    a = Store(tmp_path)
    job = a.create_job("cli", {"prompt": "Create fixture"})
    text = "REAL_RESULT\n" * 9000
    a.update_job(job["id"], status="completed", result={"text": text}, pid=123)
    b = Store(tmp_path)
    assert b.job(job["id"])["result"]["text"] == text
    assert b.job("unknown") is None


def test_concurrent_jobs_are_atomic(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda i: store.create_job("test", {"i": i})["id"], range(32)))
    assert len(set(ids)) == 32
    assert len(store.history()) == 32


def test_cli_writers_wait_for_overlapping_project_but_reviewers_and_siblings_run(tmp_path):
    store = Store(tmp_path / "state")
    project = tmp_path / "project"
    nested = project / "nested"
    sibling = tmp_path / "sibling"
    alias = tmp_path / "project-alias"
    nested.mkdir(parents=True)
    sibling.mkdir()
    alias.symlink_to(project, target_is_directory=True)
    first = store.create_job("cli", {"project": str(project), "permission": "workspace-write"})

    for path in (project, nested, alias):
        with pytest.raises(ConflictError, match=first["id"]):
            store.create_job("cli", {"project": str(path), "permission": "default"})
    reviewer = store.create_job("cli", {"project": str(project), "permission": "read-only"})
    other = store.create_job("cli", {"project": str(sibling), "permission": "workspace-write"})
    assert reviewer["status"] == other["status"] == "queued"

    store.update_job(first["id"], status="completed")
    next_writer = store.create_job("cli", {"project": str(nested), "permission": "accept-edits"})
    assert next_writer["status"] == "queued"
    with pytest.raises(ConflictError, match=next_writer["id"]):
        store.create_job("cli", {"project": str(project), "permission": "workspace-write"})


def test_concurrent_cli_writer_reservations_have_one_winner(tmp_path):
    store = Store(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()

    def start_writer(_):
        try:
            return store.create_job("cli", {"project": str(project), "permission": "workspace-write"})["id"]
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(start_writer, range(8)))
    assert len([job_id for job_id in outcomes if job_id]) == 1
    assert len(store.history()) == 1


def test_queued_writer_cancel_race_cannot_release_running_writer(tmp_path):
    store = Store(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    job = store.create_job("cli", {"project": str(project), "permission": "workspace-write", "mode": "api"})
    barrier = Barrier(2)

    def register_worker():
        barrier.wait()
        try:
            return store.update_job(job["id"], status="running", pid=os.getpid())
        except ConflictError:
            return None

    def cancel_writer():
        barrier.wait()
        return store.request_cli_cancel(job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(register_worker)
        cancel = pool.submit(cancel_writer)
        worker.result()
        cancel.result()

    final = store.job(job["id"])
    assert final["cancelRequested"] is True
    if final["status"] == "cancelled":
        assert final["pid"] is None
        assert store.create_job("cli", {"project": str(project), "permission": "workspace-write"})["status"] == "queued"
    else:
        assert final["status"] == "running" and final["pid"] == os.getpid()
        with pytest.raises(ConflictError, match=job["id"]):
            store.create_job("cli", {"project": str(project), "permission": "workspace-write"})


def test_secret_markers_redacted_in_events_and_results(tmp_path):
    store = Store(tmp_path)
    job = store.create_job("test", {"prompt": "fixture"})
    secret = "sk-" + "SYNTHETIC_TEST_VALUE_" * 3
    store.event(job["id"], "failure", {"message": "Authorization: Bearer " + secret})
    store.update_job(job["id"], result={"text": secret})
    assert secret not in json.dumps(store.events(job["id"]))
    assert secret not in json.dumps(store.job(job["id"]))
