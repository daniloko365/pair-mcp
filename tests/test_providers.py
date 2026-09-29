import asyncio
import json

import httpx2
import pytest

from pair_core.providers import Catalog, ProviderRouter, RoutingError


class VaultFixture:
    def get(self, provider_id):
        return "fixture-private-key"


def provider(pid="a", **fields):
    return {"id": pid, "name": pid, "protocol": "openai", "enabled": True,
            "baseUrl": f"https://{pid}.example/v1", "manualModels": [], **fields}


def stream(text="hello", model="actual-model", usage=True):
    chunks = [{"id": "r", "object": "chat.completion.chunk", "created": 0,
               "model": model, "choices": [{"index": 0, "delta": {"content": text}}]}]
    if usage:
        chunks.append({"id": "r", "object": "chat.completion.chunk", "created": 0,
                       "model": model, "choices": [],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
    body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.asyncio
async def test_catalog_merges_manual_and_live_pricing_capabilities():
    def handle(request):
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"data": [{"id": "x", "name": "X", "context_length": 8000,
          "pricing": {"prompt": "0.000001", "completion": "0.000003"},
          "supported_parameters": ["reasoning"], "architecture": {"output_modalities": ["text"]}}]})
    models = await Catalog(VaultFixture(), transport=httpx2.MockTransport(handle)).models(
        provider(manualModels=["x", "manual"]))
    assert [m["id"] for m in models] == ["x", "manual"]
    assert models[0]["pricing"]["inputUsdPerToken"] == 0.000001
    assert models[0]["contextLength"] == 8000
    assert models[0]["reasoning"] == []
    assert models[0]["capabilities"]["reasoningSupported"] is True
    assert models[0]["capabilities"]["reasoningEnumKnown"] is False
    assert models[1]["source"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic", "jev"])
@pytest.mark.parametrize("failure, expected", [
    (httpx2.ConnectError, "catalog_connection_error"),
    (httpx2.ConnectTimeout, "catalog_timeout"),
])
async def test_catalog_transport_failure_has_safe_typed_code(protocol, failure, expected):
    marker = "fixture-private-key secret upstream detail"

    def handle(request):
        raise failure(marker)

    catalog = Catalog(VaultFixture(), transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError) as caught:
        await catalog.models(provider(protocol=protocol))
    assert caught.value.code == expected
    assert marker not in str(caught.value)


@pytest.mark.asyncio
async def test_catalog_http_error_keeps_status_code():
    catalog = Catalog(VaultFixture(), transport=httpx2.MockTransport(
        lambda request: httpx2.Response(401, json={"error": "fixture-private-key secret"})))
    with pytest.raises(RoutingError) as caught:
        await catalog.models(provider())
    assert caught.value.code == "catalog_http_401"


@pytest.mark.asyncio
async def test_route_fallback_is_explicit_no_sdk_retry_and_actual_model():
    calls = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        calls.append(request.url.host)
        if request.url.host == "a.example":
            return httpx2.Response(429, json={"error": {"message": "fixture-private-key secret"}})
        return stream()
    router = ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "selected"},
                                    {"providerId": "b", "model": "backup"}],
                                   [{"role": "user", "content": "hello"}], {})
    assert calls == ["a.example", "b.example"]
    assert result["model"] == "actual-model"
    assert result["selectedModel"] == "backup"
    assert result["costUsd"] is None
    assert result["costStatus"] == "unknown"
    assert "fixture-private-key" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unknown_pricing_with_finite_budget_never_generates():
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json={"data": []})
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]},
                            transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError) as exc:
        await router.complete([{"providerId": "a", "model": "manual"}],
                              [{"role": "user", "content": "q"}], {"budgetUsd": 1})
    assert exc.value.code == "budget_pricing_unknown"
    assert calls == ["/v1/models"]


@pytest.mark.asyncio
async def test_budget_derived_cap_is_sent_and_disclosed():
    bodies = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {
                "prompt": "0.000001", "completion": "0.000002"}}]})
        bodies.append(json.loads(request.content))
        return stream(model="x")
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]},
                            transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "x"}],
                                  [{"role": "user", "content": "q"}], {"budgetUsd": 0.01})
    assert 0 < bodies[0]["max_completion_tokens"] < 5000
    assert any("budget-derived" in w for w in result["warnings"])
    assert result["costUsd"] == pytest.approx(0.000016)


class BrokenAfterOutput(httpx2.AsyncByteStream):
    async def __aiter__(self):
        body = stream(text="partial").content.split(b"data: [DONE]")[0]
        yield body
        raise httpx2.ReadError("fixture-private-key should never surface")


@pytest.mark.asyncio
async def test_no_fallback_after_useful_output_partial_is_preserved():
    calls = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        calls.append(request.url.host)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenAfterOutput())
    router = ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError) as exc:
        await router.complete([{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}],
                              [{"role": "user", "content": "q"}], {})
    assert calls == ["a.example"]
    assert exc.value.partial["text"] == "partial"
    assert "fixture-private-key" not in str(exc.value)


@pytest.mark.asyncio
async def test_streamed_429_after_useful_output_retains_unknown_charge_without_fallback():
    calls, events = [], []

    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {
                "prompt": "0.000001", "completion": "0.000001"}}]})
        calls.append(request.url.host)
        first = stream(text="partial", model="x", usage=False).content.split(b"data: [DONE]")[0]
        error = b'data: {"error":{"code":429,"message":"fixture rate limit"}}\n\n'
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=first + error)

    router = ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError) as exc:
        await router.complete([{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}],
                              [{"role": "user", "content": "q"}], {"maxTokens": 20, "budgetUsd": 0.02},
                              on_event=events.append)
    assert calls == ["a.example"]
    assert exc.value.code == "http_429"
    assert exc.value.partial["text"] == "partial"
    assert exc.value.reserved_usd > 0
    assert exc.value.partial["costStatus"] == "unknown"
    failure = next(event for event in events if event["type"] == "attempt_error")
    assert failure["costUsd"] is None and failure["reservedUsd"] > 0


class Delayed(httpx2.AsyncByteStream):
    async def __aiter__(self):
        # A role-only chunk does not satisfy the first-useful-output deadline.
        yield b'data: {"id":"r","object":"chat.completion.chunk","created":0,"model":"x","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        await asyncio.sleep(0.1)
        yield stream().content


@pytest.mark.asyncio
async def test_clodex_deadline_ignores_role_chunks(monkeypatch):
    monkeypatch.setattr("pair_core.providers.CLODEX_TTFT_SECONDS", 0.01)
    calls = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        calls.append(request.url.host)
        if request.url.host == "a.example":
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed())
        return stream()
    router = ProviderRouter(VaultFixture(), {"providers": [provider("a", name="Clodex"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}],
                                  [{"role": "user", "content": "q"}], {})
    assert calls == ["a.example", "b.example"]
    assert result["attempts"][0]["code"] == "first_output_timeout"


@pytest.mark.asyncio
async def test_endpoint_from_route_input_cannot_override_saved_endpoint():
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]})
    with pytest.raises(RoutingError, match="route_fields"):
        await router.complete([{"providerId": "a", "model": "x", "baseUrl": "http://169.254.169.254"}],
                              [{"role": "user", "content": "q"}], {})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [499, 500, 503])
async def test_transient_fallback_keeps_unknown_outcome_reserve(status):
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0.000001", "completion": "0.000001"}}]})
        if request.url.host == "a.example":
            return httpx2.Response(status, json={"error": {"message": "unavailable"}})
        return stream(model="x")
    router = ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}],
                                  [{"role": "user", "content": "q"}], {"maxTokens": 20, "budgetUsd": 0.02})
    assert result["reservedUsd"] > 0
    assert result["costStatus"] == "unknown"
    assert len(result["attempts"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_non_transient_errors_do_not_trigger_fallback(status):
    calls = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        calls.append(request.url.host)
        return httpx2.Response(status, json={"error": {"message": "fixture-private-key"}})
    router = ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]},
                            transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError):
        await router.complete([{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}],
                              [{"role": "user", "content": "q"}], {})
    assert calls == ["a.example"]


@pytest.mark.asyncio
async def test_actual_model_mismatch_never_uses_selected_price():
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0.000001", "completion": "0.000001"}}]})
        return stream(model="unknown-expensive-model")
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "x"}],
                                  [{"role": "user", "content": "q"}], {"maxTokens": 20, "budgetUsd": 0.02})
    assert result["costUsd"] is None
    assert result["reservedUsd"] > 0


@pytest.mark.asyncio
async def test_time_limit_keeps_started_request_reservation():
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0.000001", "completion": "0.000001"}}]})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed())
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle))
    with pytest.raises(RoutingError) as exc:
        await router.complete([{"providerId": "a", "model": "x"}],
                              [{"role": "user", "content": "q"}], {"maxTokens": 20, "budgetUsd": 0.02, "timeSeconds": 0.02})
    assert exc.value.code == "time_limit"
    assert exc.value.reserved_usd > 0


@pytest.mark.asyncio
async def test_anthropic_stream_is_parsed_by_official_sdk():
    bodies = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "claude-opus", "type": "model", "display_name": "Opus", "created_at": "2026-01-01T00:00:00Z"}], "has_more": False})
        assert request.url.path == "/v1/messages"
        bodies.append(json.loads(request.content))
        events = [
          ("message_start", {"type": "message_start", "message": {"id": "r", "type": "message", "role": "assistant", "model": "claude-opus", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
          ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "design"}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
          ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
          ("message_stop", {"type": "message_stop"})]
        payload = "".join("event: " + name + "\ndata: " + json.dumps(data) + "\n\n" for name, data in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=payload)
    router = ProviderRouter(VaultFixture(), {"providers": [provider(protocol="anthropic", baseUrl="https://a.example")]},
                            transport=httpx2.MockTransport(handle))
    result = await router.complete([{"providerId": "a", "model": "claude-opus", "effort": "high"}],
                                  [{"role": "system", "content": "be helpful"}, {"role": "user", "content": "q"}], {"maxTokens": 2000})
    assert result["text"] == "design"
    assert result["model"] == "claude-opus"
    assert result["usage"]["outputTokens"] == 2
    assert bodies[0]["system"] == "be helpful"
    assert bodies[0]["max_tokens"] == 2000
    assert bodies[0]["output_config"]["effort"] == "high"


@pytest.mark.asyncio
async def test_budget_zero_accepts_only_catalog_explicitly_free_model():
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "free", "pricing": {"prompt": "0", "completion": "0"}}]})
        return stream(model="free")
    result = await ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle)).complete(
        [{"providerId": "a", "model": "free"}], [{"role": "user", "content": "q"}], {"budgetUsd": 0})
    assert result["costUsd"] == 0
    assert result["costBasis"] == "catalog_estimate"


@pytest.mark.asyncio
async def test_streamed_503_before_output_can_fall_back():
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        if request.url.host == "a.example":
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"error":{"code":503,"message":"fixture-private-key"}}\n\n')
        return stream()
    result = await ProviderRouter(VaultFixture(), {"providers": [provider("a"), provider("b")]}, transport=httpx2.MockTransport(handle)).complete(
        [{"providerId": "a", "model": "x"}, {"providerId": "b", "model": "x"}], [{"role": "user", "content": "q"}], {})
    assert result["attempts"][0]["code"] == "http_503"
    assert result["providerId"] == "b"


@pytest.mark.asyncio
async def test_request_reservation_event_precedes_generation_and_cancel_retains_it():
    active = asyncio.Event()
    events = []
    async def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0.000001", "completion": "0.000001"}}]})
        assert events and events[-1]["type"] == "attempt_start"
        assert events[-1]["reservedUsd"] > 0
        active.set()
        await asyncio.sleep(100)
    router = ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle))
    task = asyncio.create_task(router.complete([{"providerId": "a", "model": "x"}], [{"role": "user", "content": "q"}],
        {"maxTokens": 20, "budgetUsd": 0.02}, on_event=events.append))
    await asyncio.wait_for(active.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1]["type"] == "attempt_cancelled"
    started = next(event for event in events if event["type"] == "attempt_start")
    assert events[-1]["reservedUsd"] == started["reservedUsd"]


@pytest.mark.asyncio
async def test_catalog_ttl_cache_key_endpoint_and_manual_snapshot_invalidation():
    now = [0]
    calls = []
    class ChangingVault:
        current = "fixture-first-key"
        def get(self, provider_id):
            return self.current
    vault = ChangingVault()
    def handle(request):
        calls.append(request.url.host)
        return httpx2.Response(200, json={"data": [{"id": "live"}]})
    catalog = Catalog(vault, transport=httpx2.MockTransport(handle), clock=lambda: now[0], ttl_seconds=60)
    saved = provider()
    first = await catalog.models(saved)
    first[0]["name"] = "external mutation"
    second = await catalog.models(saved)
    assert len(calls) == 1
    assert second[0]["name"] == "live"
    assert second[0]["catalog"]["cached"] is True
    vault.current = "fixture-second-key"
    await catalog.models(saved)
    assert len(calls) == 2
    saved["baseUrl"] = "https://changed.example/v1"
    await catalog.models(saved)
    assert len(calls) == 3
    saved["manualModels"] = ["manual"]
    assert len(await catalog.models(saved)) == 2
    assert len(calls) == 4
    now[0] = 61
    await catalog.models(saved)
    assert len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("pricing, reserve_input, reserve_output", [
    ({"prompt": "0.0000001", "completion": "0.0000005", "web_search": "0.01",
      "input_cache_read": "0.00000001", "input_cache_write": "0.000000125",
      "overrides": [{"min_prompt_tokens": 272000, "prompt": "0.0000002", "completion": "0.00000075",
                     "input_cache_read": "0.00000002", "input_cache_write": "0.00000025"}]}, 0.00000025, 0.00000075),
    ({"prompt": "0.000004", "completion": "0.00002", "web_search": "0.01",
      "input_cache_write_1h": "0.000008"}, 0.000008, 0.00002),
])
async def test_plaintext_catalog_optional_search_and_cache_tiers_use_conservative_budget(pricing, reserve_input, reserve_output):
    posts = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": pricing}]})
        posts.append(json.loads(request.content))
        return stream(model="x")
    catalog = Catalog(VaultFixture(), transport=httpx2.MockTransport(handle))
    model = (await catalog.models(provider()))[0]
    assert model["pricing"]["inputUsdPerToken"] == float(pricing["prompt"])
    assert model["pricing"]["reserveInputUsdPerToken"] == reserve_input
    assert model["pricing"]["reserveOutputUsdPerToken"] == reserve_output
    result = await ProviderRouter(VaultFixture(), {"providers": [provider()]}, catalog=catalog,
                                  transport=httpx2.MockTransport(handle)).complete(
        [{"providerId": "a", "model": "x"}], [{"role": "user", "content": "q"}], {"budgetUsd": 0.01, "maxTokens": 32})
    assert "tools" not in posts[0] and "plugins" not in posts[0]
    assert result["costUsd"] == pytest.approx(10 * reserve_input + 3 * reserve_output)
    assert result["costBasis"] == "conservative_catalog_estimate"


@pytest.mark.asyncio
async def test_mandatory_request_charge_is_reserved_and_estimated():
    events = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0", "completion": "0", "request": "0.005"}}]})
        return stream(model="x")
    result = await ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle)).complete(
        [{"providerId": "a", "model": "x"}], [{"role": "user", "content": "q"}], {"budgetUsd": 0.01}, on_event=events.append)
    assert next(e for e in events if e["type"] == "attempt_start")["reservedUsd"] == pytest.approx(0.005)
    assert result["costUsd"] == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_unknown_required_positive_pricing_still_blocks_paid_request():
    posts = []
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "x", "pricing": {"prompt": "0.000001", "completion": "0.000001", "mystery_required_fee": "0.02"}}]})
        posts.append(request)
        return stream(model="x")
    with pytest.raises(RoutingError, match="budget_pricing_unknown"):
        await ProviderRouter(VaultFixture(), {"providers": [provider()]}, transport=httpx2.MockTransport(handle)).complete(
            [{"providerId": "a", "model": "x"}], [{"role": "user", "content": "q"}], {"budgetUsd": 0.01})
    assert not posts
