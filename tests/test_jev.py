import json

import httpx2
import pytest

from pair_core.jev import JevTools
from pair_core.providers import RoutingError


class VaultFixture:
    def get(self, provider_id):
        return "fixture-private-key"


def config():
    return {"providers": [{"id": "j", "protocol": "jev", "baseUrl": "https://api.typesafe.ai", "enabled": True}],
            "jev": {"enabled": True, "providerId": "j", "model": "jev-latest"}}


@pytest.mark.asyncio
async def test_check_returns_real_typed_answer_and_raw_input_reference():
    def handle(request):
        assert request.url.path == "/v1/systemone"
        payload = json.loads(request.content)
        assert payload["questions"]["check"]["type"] == "noul"
        return httpx2.Response(200, json={"model": "jev-1.13", "answers": {"check": {"type": "noul", "noul": 0.6}},
            "usage": {"input_tokens": 15, "output_tokens": 2}})
    result = await JevTools(VaultFixture(), config(), transport=httpx2.MockTransport(handle)).check("raw", "Relevant?")
    assert result["answer"]["noul"] == 0.6
    assert result["uncertain"] is True
    assert result["rawInput"] == "raw"
    assert result["model"] == "jev-1.13"


@pytest.mark.asyncio
async def test_rank_retains_mandatory_and_returns_rank_not_destructive_selection():
    def handle(request):
        return httpx2.Response(200, json={"model": "jev-1.13", "answers": {"rank": {
            "type": "choice", "choice": "b", "confidence": 0.8, "probabilities": {"a": 0.1, "b": 0.9}}},
            "usage": {"input_tokens": 20, "output_tokens": 3}})
    candidates = [{"id": "a", "description": "mandatory skill", "mandatory": True}, {"id": "b", "description": "other"}]
    result = await JevTools(VaultFixture(), config(), transport=httpx2.MockTransport(handle)).rank("task", candidates)
    assert result["ranked"][0]["id"] == "b"
    assert result["mandatoryIds"] == ["a"]
    assert result["rawCandidates"] == candidates
    assert len(result["ranked"]) == 2


@pytest.mark.asyncio
async def test_missing_key_is_actionable_and_no_request():
    class EmptyVault:
        def get(self, provider_id):
            return None
    with pytest.raises(RoutingError, match="missing_key"):
        await JevTools(EmptyVault(), config()).check("raw", "Relevant?")


@pytest.mark.asyncio
async def test_bad_probabilities_are_not_presented_as_valid_rank():
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={"model": "jev", "answers": {"rank": {
        "type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 1.8}}}}))
    with pytest.raises(RoutingError, match="jev_invalid_answer"):
        await JevTools(VaultFixture(), config(), transport=transport).rank("task", [{"id": "a"}])


@pytest.mark.asyncio
async def test_disabled_jev_does_not_silently_make_a_paid_call():
    settings = config()
    settings["jev"]["enabled"] = False
    with pytest.raises(RoutingError, match="jev_disabled"):
        await JevTools(VaultFixture(), settings).check("raw", "Relevant?")


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://openrouter.ai/api", "https://openrouter.ai/api/v1"])
async def test_openrouter_uses_typed_decisions_and_reported_cost(base):
    settings = config()
    settings["providers"][0]["baseUrl"] = base
    settings["jev"]["model"] = "typesafe/jev-1.13"
    def handle(request):
        assert request.url.path == "/api/alpha/decisions"
        body = json.loads(request.content)
        assert body["model"] == "typesafe/jev-1.13"
        assert "messages" not in body
        return httpx2.Response(200, json={"model": "typesafe/jev-1.13-20260917", "answers": {
            "check": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 492, "output_tokens": 38, "cost": 0.000020664}, "provider": "TypeSafe"})
    result = await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert result["costUsd"] == pytest.approx(0.000020664)
    assert result["costBasis"] == "provider_reported"
    assert result["model"] == "typesafe/jev-1.13-20260917"


@pytest.mark.asyncio
async def test_jev_finite_budget_cannot_call_unknown_pricing():
    settings = config()
    settings["limits"] = {"budgetUsd": 0}
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json={"data": [{"id": "jev-latest"}]})
    with pytest.raises(RoutingError, match="budget_pricing_unknown"):
        await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert calls == ["/v1/models"]


@pytest.mark.asyncio
async def test_jev_reuses_openrouter_key_with_finite_live_catalog_budget():
    settings = config()
    settings["providers"][0].update(protocol="openai", baseUrl="https://openrouter.ai/api/v1")
    settings["jev"]["model"] = "typesafe/jev-1.13"
    settings["limits"] = {"budgetUsd": 0.01}
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "typesafe/jev-1.13", "pricing": {"prompt": "0.000000042", "completion": "0"}}]})
        return httpx2.Response(200, json={"model": "typesafe/jev-1.13-20260917", "answers": {"check": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.0000042}})
    result = await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert calls == ["/api/v1/models", "/api/alpha/decisions"]
    assert result["costUsd"] == pytest.approx(0.0000042)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens", [True, -10])
async def test_jev_invalid_usage_tokens_cannot_be_priced_as_known(input_tokens):
    settings = config()
    settings["limits"] = {"budgetUsd": 0.1}
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "jev-latest", "pricing": {"prompt": "0.00001", "completion": "0"}}]})
        return httpx2.Response(200, json={"model": "jev-latest", "answers": {"check": {"type": "noul", "noul": 0.9}},
                                        "usage": {"input_tokens": input_tokens}})
    result = await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert result["costUsd"] is None
    assert result["reservedUsd"] > 0


@pytest.mark.asyncio
async def test_jev_changed_actual_model_without_reported_cost_retains_reserve():
    settings = config()
    settings["limits"] = {"budgetUsd": 0.1}
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "jev-latest", "pricing": {"prompt": "0.00001", "completion": "0"}}]})
        return httpx2.Response(200, json={"model": "different-expensive-model", "answers": {"check": {"type": "noul", "noul": 0.9}},
                                        "usage": {"input_tokens": 50}})
    result = await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert result["costUsd"] is None
    assert result["reservedUsd"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_cost", [True, -0.1, float("nan"), float("inf")])
async def test_jev_invalid_reported_cost_is_not_a_known_charge(reported_cost):
    settings = config()
    settings["limits"] = {"budgetUsd": 0.1}
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "jev-latest", "pricing": {"prompt": "0.00001", "completion": "0"}}]})
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=json.dumps({
            "model": "other-model", "answers": {"check": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 50, "cost": reported_cost}}))
    result = await JevTools(VaultFixture(), settings, transport=httpx2.MockTransport(handle)).check("state", "Relevant?")
    assert result["costUsd"] is None
    assert result["reservedUsd"] > 0
