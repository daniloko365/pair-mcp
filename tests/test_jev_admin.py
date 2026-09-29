import json
from functools import partial

import httpx2
import pytest
from starlette.testclient import TestClient

from pair_core.jev import JevTools
from pair_core.service import Service
from pair_core.store import Store


STATE = "Public fixture context for choosing optional input."
CANDIDATES = [
    {"id": "mandatory", "description": "Required safety context", "mandatory": True},
    {"id": "optional", "description": "Relevant optional context"},
    {"id": "explicit", "description": "Owner-requested context", "explicit": True},
]


class FixtureVault:
    def get(self, provider_id):
        assert provider_id == "j"
        return "fixture-not-a-real-key"

    def has(self, provider_id):
        return provider_id == "j"


class FixtureCli:
    async def status(self):
        return {"codex": {"available": False, "quotas": None}}


def auth():
    return {"Authorization": "Bearer fixture-local-token"}


@pytest.fixture
def jev_client(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        calls.append(body)
        if "rank" in body["questions"]:
            answers = {"rank": {"type": "choice", "choice": "optional", "confidence": 0.9,
                "probabilities": {"mandatory": 0.1, "optional": 0.8, "explicit": 0.1}}}
        else:
            answers = {"check": {"type": "noul", "noul": 0.95}}
        return httpx2.Response(200, json={"model": "jev-fixture", "answers": answers,
            "usage": {"input_tokens": 30, "output_tokens": 5, "cost": 0.00001}})

    monkeypatch.setattr("pair_core.jev.JevTools", partial(JevTools, transport=httpx2.MockTransport(handle)))
    store = Store(tmp_path)
    cfg = store.config()
    cfg["providers"] = [{"id": "j", "name": "Fixture Jev", "protocol": "jev",
        "baseUrl": "https://api.typesafe.ai", "enabled": True}]
    cfg["jev"] = {"enabled": True, "providerId": "j", "model": "jev-fixture"}
    store.save_config(cfg)
    with TestClient(Service(store, FixtureVault(), cli=FixtureCli()).app("fixture-local-token"),
                    base_url="http://127.0.0.1") as api:
        yield api, store, calls


def assert_completed_job(api, store, response):
    assert response.status_code == 200, response.json()
    result = response.json()
    job = store.job(result["jobId"])
    assert len(store.history()) == 1
    assert job["kind"] == "jev"
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["costUsd"] == pytest.approx(0.00001)
    assert job["reservedUsd"] == 0
    assert job["result"]["rawInput"] == STATE
    assert api.get("/jobs/" + job["id"], headers=auth()).json() == job
    return result, job


@pytest.mark.parametrize("options, expected_instructions", [
    ({"question": "Choose relevant optional context without dropping required context."},
     "Choose relevant optional context without dropping required context."),
    ({}, "Rank relevance to the task"),
    ({"instructions": "Use the existing direct admin instruction.", "threshold": 0.95},
     "Use the existing direct admin instruction."),
], ids=["mcp-question", "default-instructions", "direct-instructions-threshold"])
def test_rank_maps_question_and_retains_raw_required_context(jev_client, options, expected_instructions):
    api, store, calls = jev_client
    response = api.post("/jev", headers=auth(), json={"operation": "rank", "state": STATE,
        "candidates": CANDIDATES, **options})
    result, job = assert_completed_job(api, store, response)
    assert len(calls) == 1
    assert calls[0]["state"] == STATE
    assert calls[0]["questions"]["rank"]["instructions"] == expected_instructions
    assert calls[0]["questions"]["rank"]["criteria"] == {
        item["id"]: item["description"] for item in CANDIDATES}
    assert result["rawCandidates"] == CANDIDATES
    assert job["result"]["rawCandidates"] == CANDIDATES
    assert result["mandatoryIds"] == ["mandatory", "explicit"]
    assert result["uncertain"] is ("threshold" in options)
    assert {item["id"] for item in result["ranked"]} == {item["id"] for item in CANDIDATES}
    assert result["selectionApplied"] is False


def test_check_question_without_candidates_uses_typed_check(jev_client):
    api, store, calls = jev_client
    question = "Is the supplied public context relevant?"
    response = api.post("/jev", headers=auth(), json={"operation": "check", "state": STATE,
        "question": question, "threshold": 0.98})
    result, _ = assert_completed_job(api, store, response)
    assert len(calls) == 1
    assert calls[0]["questions"] == {"check": {"type": "noul", "instructions": question}}
    assert result["decision"] is None
    assert result["uncertain"] is True
    assert result["rawInput"] == STATE


@pytest.mark.parametrize("body", [
    [],
    {"operation": "unknown", "state": STATE},
    {"operation": "check", "question": "Relevant?"},
    {"operation": "check", "state": None, "question": "Relevant?"},
    {"operation": "check", "state": STATE},
    {"operation": "check", "state": STATE, "question": ""},
    {"operation": "check", "state": STATE, "question": "Relevant?", "candidates": CANDIDATES},
    {"operation": "check", "state": STATE, "question": "Relevant?", "providerId": "unconfigured"},
    {"operation": "rank", "state": STATE},
    {"operation": "rank", "state": STATE, "candidates": "not-a-list"},
    {"operation": "rank", "state": STATE, "candidates": CANDIDATES, "question": 17},
    {"operation": "rank", "state": STATE, "candidates": CANDIDATES,
     "question": "First instruction", "instructions": "Conflicting instruction"},
], ids=["non-object", "unknown-operation", "missing-state", "invalid-state", "missing-check-question",
        "blank-check-question", "check-candidates", "unknown-field", "missing-rank-candidates",
        "invalid-rank-candidates", "invalid-rank-question", "ambiguous-rank-instructions"])
def test_invalid_request_rejected_before_upstream_or_job(jev_client, body):
    api, store, calls = jev_client
    response = api.post("/jev", headers=auth(), json=body)
    assert response.status_code == 400, response.json()
    assert calls == []
    assert store.history() == []


@pytest.mark.parametrize("headers, expected_status", [
    ({}, 401),
    ({"Authorization": "Bearer wrong-local-token"}, 401),
    ({**auth(), "Origin": "https://untrusted.example"}, 403),
    ({**auth(), "Host": "untrusted.example"}, 403),
], ids=["no-auth", "wrong-auth", "browser-origin", "foreign-host"])
def test_jev_preserves_local_authorization_and_origin_guards(jev_client, headers, expected_status):
    api, store, calls = jev_client
    response = api.post("/jev", headers=headers,
        json={"operation": "check", "state": STATE, "question": "Relevant?"})
    assert response.status_code == expected_status
    assert calls == []
    assert store.history() == []
