import pytest

from pair_core.council import CouncilRunner


class StoreFixture:
    def __init__(self, config):
        self.saved = config
    def config(self):
        return self.saved


class RouterFixture:
    def __init__(self):
        self.requests = []
    async def complete(self, routes, messages, limits, **kwargs):
        self.requests.append((routes, messages, limits))
        return {"text": routes[0]["model"], "providerId": routes[0]["providerId"],
                "model": routes[0]["model"], "costUsd": 0.1, "reservedUsd": 0,
                "attempts": [], "warnings": []}


@pytest.mark.asyncio
async def test_independent_opinions_and_host_synthesis_is_pending():
    router = RouterFixture()
    roster = [{"id": str(i), "label": f"Role {i}", "routes": [{"providerId": "a", "model": f"m{i}"}]} for i in range(2)]
    runner = CouncilRunner(StoreFixture({"members": roster, "limits": {}}), object(), router=router)
    result = await runner.run("Design?", context="facts")
    assert len(result["opinions"]) == 2
    assert result["synthesis"]["status"] == "host_pending"
    assert result["status"] == "awaiting_host_synthesis"
    assert "confidence" not in result
    assert all("facts" in req[1][-1]["content"] for req in router.requests)
    assert "m0" not in router.requests[1][1][-1]["content"]


@pytest.mark.asyncio
async def test_synthesis_route_and_combined_budget_are_explicit():
    router = RouterFixture()
    roster = [{"id": str(i), "label": "review", "routes": [{"providerId": "a", "model": f"m{i}"}]} for i in range(2)]
    config = {"members": roster, "limits": {"budgetUsd": 3},
              "synthesis": {"providerId": "a", "model": "synth"}}
    result = await CouncilRunner(StoreFixture(config), object(), router=router).run("Question")
    assert result["synthesis"]["text"] == "synth"
    assert result["status"] == "completed"
    assert sum(req[2]["budgetUsd"] for req in router.requests) == 3
    assert "m0" in router.requests[-1][1][-1]["content"]
    assert result["costUsd"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_subscription_council_uses_scoped_read_only_official_cli():
    class CliFixture:
        def __init__(self):
            self.calls = []
        async def start(self, agent, prompt, project, **kwargs):
            self.calls.append((agent, project, kwargs))
            return {"id": "job", "status": "completed", "result": {"text": "Opinion", "model": "actual-sol", "usage": {"inputTokens": 10}}}
    cfg = {"members": [{"id": "s", "label": "Implementation", "routes": [{"providerId": "codex", "model": "selected-sol", "effort": "high"}]}],
           "pair": {"projectRoots": ["/explicit/project"]}, "limits": {"budgetUsd": 0}}
    cli = CliFixture()
    result = await CouncilRunner(StoreFixture(cfg), object(), cli=cli).run("Review?")
    assert cli.calls[0][1] == "/explicit/project"
    assert cli.calls[0][2]["permission"] == "read-only"
    assert cli.calls[0][2]["mode"] == "subscription"
    assert result["opinions"][0]["model"] == "actual-sol"
    assert result["costUsd"] == 0


@pytest.mark.asyncio
async def test_subscription_ambiguous_project_fails_before_dispatch():
    cfg = {"members": [{"id": "s", "label": "Review", "routes": [{"providerId": "codex", "model": "sol"}]}],
           "pair": {"projectRoots": ["/a", "/b"]}, "limits": {}}
    result = await CouncilRunner(StoreFixture(cfg), object(), cli=object()).run("Review?")
    assert result["status"] == "failed"
    assert result["errors"][0]["error"]["code"] == "subscription_council_requires_explicit_project"


@pytest.mark.asyncio
async def test_member_adapter_exception_preserves_other_real_opinion():
    class PartialRouter(RouterFixture):
        async def complete(self, routes, messages, limits, **kwargs):
            if routes[0]["model"] == "broken":
                raise RuntimeError("fixture-private-key")
            return await super().complete(routes, messages, limits, **kwargs)
    roster = [{"id": model, "label": model, "routes": [{"providerId": "a", "model": model}]} for model in ["good", "broken"]]
    result = await CouncilRunner(StoreFixture({"members": roster, "limits": {}}), object(), router=PartialRouter()).run("Review?")
    assert result["opinions"][0]["text"] == "good"
    assert result["errors"][0]["error"]["code"] == "member_error"
    assert "fixture-private-key" not in str(result)


@pytest.mark.asyncio
async def test_cancellation_preserves_settled_opinion_and_active_reserve():
    import asyncio
    from pair_core.council import CouncilCancelled
    active = asyncio.Event()
    class MeteredRouter(RouterFixture):
        async def complete(self, routes, messages, limits, **kwargs):
            if routes[0]["model"] == "paid":
                return await super().complete(routes, messages, limits, **kwargs)
            callback = kwargs.get("on_event")
            assert callback is not None
            await callback({"type": "attempt_start", "attemptId": "active-attempt", "status": "running", "reservedUsd": 0.4, "costUsd": None})
            active.set()
            await asyncio.sleep(100)
    roster = [{"id": model, "label": model, "routes": [{"providerId": "a", "model": model}]} for model in ["paid", "active"]]
    events = []
    runner = CouncilRunner(StoreFixture({"members": roster, "limits": {"budgetUsd": 1}}), object(), router=MeteredRouter())
    task = asyncio.create_task(runner.run("Review?", on_event=events.append))
    await active.wait()
    task.cancel()
    with pytest.raises(CouncilCancelled) as exc:
        await task
    partial = exc.value.public()
    assert partial["knownCostUsd"] == pytest.approx(0.1)
    assert partial["reservedUsd"] == pytest.approx(0.4)
    assert partial["opinions"][0]["text"] == "paid"
    assert partial["costUsd"] is None
    assert any(event.get("memberId") == "active" and event["type"] == "attempt_start" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "failed", "interrupted", "timed_out"])
async def test_partner_terminal_state_is_not_paid_api_fallback(terminal):
    class CliFixture:
        async def start(self, *args, **kwargs):
            return {"id": "child", "status": terminal, "result": {"text": ""}}
    router = RouterFixture()
    cfg = {"members": [{"id": "s", "label": "Review", "routes": [{"providerId": "codex", "model": "sol"}, {"providerId": "api", "model": "paid"}]}],
           "pair": {"projectRoots": ["/explicit/project"]}, "limits": {"budgetUsd": 1}}
    result = await CouncilRunner(StoreFixture(cfg), object(), cli=CliFixture(), router=router).run("Review?")
    assert not router.requests
    assert result["errors"][0]["error"]["code"] == "partner_job_" + terminal


@pytest.mark.asyncio
async def test_cancelling_partner_is_polled_until_terminal_without_api_fallback():
    class PollStore(StoreFixture):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.polled = 0
        def job(self, jid):
            assert jid == "child"
            self.polled += 1
            return {"id": jid, "status": "cancelled", "result": {"text": ""}}
    class CliFixture:
        async def start(self, *args, **kwargs):
            return {"id": "child", "status": "cancelling"}
    router = RouterFixture()
    cfg = {"members": [{"id": "s", "label": "Review", "routes": [{"providerId": "codex", "model": "sol"}, {"providerId": "api", "model": "paid"}]}],
           "pair": {"projectRoots": ["/explicit/project"]}, "limits": {}}
    store = PollStore(cfg)
    result = await CouncilRunner(store, object(), cli=CliFixture(), router=router).run("Review?")
    assert store.polled == 1
    assert not router.requests
    assert result["errors"][0]["error"]["code"] == "partner_job_cancelled"
