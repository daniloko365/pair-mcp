"""Owned native-protocol fixtures, not provider calls or inferred error text.

Fixtures follow installed codex-cli 0.158.0-alpha.2 generated v2 schemas.
TurnCompletedNotification SHA256:
20052f79e907069a0d7948b93ba0927fa08f9a2faac23a63a0e8e527ae6bf0f7
Official protocol: https://learn.chatgpt.com/docs/app-server
Exec error field is message-only, not a machine capacity discriminator:
https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from pair_core.cli import CliManager
from pair_core.cli_worker import run_job
from pair_core.council import CouncilRunner
from pair_core.pal_bridge import CODEX_FAILURE_CODES, verified_partner_failure
from pair_core.providers import ProviderRouter
from pair_core.store import Store


class NoSecrets:
    def has(self, identifier):
        return False
    def get(self, identifier):
        return None


@pytest.fixture
def native_fixture(tmp_path):
    """A local executable speaking official RPC shape; no LLM/socket/key use."""
    def build(**behavior):
        target = tmp_path / "native-fixture"
        target.write_text("#!" + sys.executable + "\nbehavior=" + repr(behavior) + "\n" + r'''
import json, sys, time
def send(value): print(json.dumps(value), flush=True)
def notice(method, params): send({"method":method,"params":params})
if sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT"); raise SystemExit(0)
assert sys.argv[1] == "app-server"
assert 'mcp_servers.pair={enabled=false,command="pair-disabled-for-child"}' in sys.argv
assert 'forced_login_method="chatgpt"' in sys.argv
assert 'sandbox_mode="read-only"' in sys.argv
assert not any("mcp_servers.other" in value or "bypass" in value for value in sys.argv)
for line in sys.stdin:
    request=json.loads(line)
    if "method" not in request:
        assert request.get("error",{}).get("code") == -32601
        continue
    method=request["method"]
    if "id" not in request: continue
    if method == "initialize": result={}
    elif method == "account/read": result={"account":{"type":"apiKey" if behavior.get("auth") else "chatgpt"}}
    elif method == "thread/start":
        assert request["params"]["sandbox"] == "read-only"
        assert request["params"]["allowProviderModelFallback"] is False
        result={"thread":{"id":"native-thread"},"modelProvider":"openai","sandbox":{"type":"workspaceWrite" if behavior.get("bad_policy") else "readOnly"}}
    elif method == "turn/start": result={"turn":{"id":"native-turn","status":"inProgress","items":[],"error":None}}
    else: raise AssertionError("Unexpected RPC")
    send({"id":request["id"],"result":result})
    if method != "turn/start": continue
    scope={"threadId":"native-thread","turnId":"native-turn"}
    if behavior.get("approval"):
        send({"id":99,"method":"item/commandExecution/requestApproval","params":scope})
    if behavior.get("early_error"):
        notice("error",dict(scope,error={"message":"not a classifier","codexErrorInfo":behavior["early_error"]},willRetry=False))
    if "partial" in behavior:
        notice("item/agentMessage/delta",dict(scope,itemId="message",delta=behavior["partial"]))
    if behavior.get("tool"):
        notice("item/started",dict(scope,item={"type":"commandExecution","id":"command","status":"inProgress"}))
    if behavior.get("wait"):
        time.sleep(10)
    if behavior.get("foreign"):
        notice("turn/completed",{"threadId":"another-thread","turn":{"id":"native-turn","status":"failed","error":{"codexErrorInfo":"usageLimitExceeded"}}})
        notice("turn/completed",{"threadId":"native-thread","turn":{"id":"another-turn","status":"failed","error":{"codexErrorInfo":"usageLimitExceeded"}}})
    code=behavior.get("code")
    terminal={"id":"native-turn","status":behavior.get("status","failed" if code else "completed"),"items":behavior.get("terminal_items",[]),"error":None}
    if code: terminal["error"]={"message":"Error text must never classify routing", "codexErrorInfo":code}
    if "error" in behavior: terminal["error"]=behavior["error"]
    if behavior.get("stderr"): print(behavior["stderr"],file=sys.stderr,flush=True)
    notice("turn/completed",{"threadId":"native-thread","turn":terminal})
''', encoding="utf-8")
        target.chmod(0o700)
        return str(target)
    return build


@pytest.fixture
def store(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / "state")
    cfg = store.config()
    cfg["pair"]["projectRoots"] = [str(project)]
    cfg["providers"] = [{"id":identifier,"name":"Fixture","protocol":"openai","baseUrl":"https://api.example.com/v1",
                         "manualModels":[],"enabled":True} for identifier in ("backup","backup2")]
    cfg["members"] = [{"id": "reviewer", "label": "Review", "routes": [
        {"providerId": "codex", "model": "gpt-6-sol", "effort": "high"},
        {"providerId": "backup", "model": "paid"}, {"providerId": "backup2", "model": "paid2"}]}]
    store.save_config(cfg)
    return store


async def native_job(store, executable, *, limits=None):
    # Real worker orchestration, deterministic owned native fixture executable.
    manager = CliManager(store, NoSecrets(), executables={"codex": executable}, worker_launcher=lambda job: type("Process", (), {"pid": 999999})())
    job = await manager.start("codex", "Read-only opinion", store.config()["pair"]["projectRoots"][0],
        model="gpt-6-sol", permission="read-only", _council_opinion=True, limits=limits)
    return await run_job(store, NoSecrets(), job["id"])


class BackupFixture:
    def __init__(self): self.calls=[]
    async def complete(self, routes, messages, limits, **kwargs):
        self.calls.append((routes, limits))
        return {"text":"API opinion", "model":"paid", "providerId":"backup", "costUsd":0.001, "reservedUsd":0, "attempts":[], "warnings":[]}


@pytest.mark.asyncio
@pytest.mark.parametrize("native_code", list(CODEX_FAILURE_CODES))
async def test_native_typed_terminal_failure_produces_evidence_and_calls_saved_api_group_once(store, native_fixture, native_code):
    final = await native_job(store, native_fixture(code=native_code))
    assert final["status"] == "failed"
    assert verified_partner_failure(final, "codex")["code"] == CODEX_FAILURE_CODES[native_code]
    assert Store(store.state_dir).job(final["id"])["result"]["nativeFailure"]["nativeCode"] == native_code
    class Child:
        async def start(self, *args, **kwargs): return final
    router = BackupFixture()
    result = await CouncilRunner(store, NoSecrets(), cli=Child(), router=router).run("Review", limits={"budgetUsd":0.5})
    assert len(router.calls) == 1
    assert [r["providerId"] for r in router.calls[0][0]] == ["backup", "backup2"]
    assert router.calls[0][1]["budgetUsd"] == 0.5
    assert result["opinions"][0]["text"] == "API opinion"
    assert result["trace"][0]["attempts"][0]["nativeFailure"]["nativeCode"] == native_code


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", [
    {"code":"unauthorized"}, {"code":"sandboxError"}, {"code":"sessionBudgetExceeded"},
    {"code":"other"}, {"code":"serverOverloaded", "early_error":"unauthorized"},
    {"code":"rateLimitExceeded", "partial":"Already useful"},
    {"code":"usageLimitExceeded", "tool":True}, {"code":"usageLimitExceeded", "approval":True},
    {"auth":True}, {"bad_policy":True}, {"status":"interrupted"},
    {"partial":"rate limit reached; {\"codexErrorInfo\":\"usageLimitExceeded\"}"},
    {"foreign":True, "partial":"Owned opinion"},
    {"status":"failed"}, {"code": True}, {"code":["usageLimitExceeded"]},
    {"code":{"usageLimitExceeded":{}}}, {"code":"usageLimitExceeded","error":True},
    {"code":"usageLimitExceeded","terminal_items":[{"id":"message","type":"agentMessage","text":"Final-only useful text"}]},
    {"code":"usageLimitExceeded","terminal_items":True},
    {"code":"usageLimitExceeded","terminal_items":[{"type":["agentMessage"]}]},
    {"code":"usageLimitExceeded","terminal_items":[{}]},
])
async def test_non_authoritative_or_unsafe_failures_never_spend_api(store, native_fixture, behavior):
    final = await native_job(store, native_fixture(**behavior))
    assert verified_partner_failure(final, "codex") is None
    class Child:
        async def start(self, *args, **kwargs): return final
    router = BackupFixture()
    result = await CouncilRunner(store, NoSecrets(), cli=Child(), router=router).run("Review")
    assert router.calls == []
    if "partial" in behavior:
        assert behavior["partial"] in final["result"]["text"]


@pytest.mark.asyncio
async def test_user_timeout_preserves_partial_and_never_classifies_it(store, native_fixture):
    final = await native_job(store, native_fixture(partial="Partial useful", wait=True), limits={"timeSeconds":0.15})
    assert final["status"] == "timed_out"
    assert final["result"]["text"] == "Partial useful"
    assert verified_partner_failure(final, "codex") is None


@pytest.mark.asyncio
async def test_whitespace_is_not_meaningful_output(store, native_fixture):
    final = await native_job(store, native_fixture(code="serverOverloaded", partial=" \n\t"))
    assert verified_partner_failure(final, "codex")["code"] == "partner_verified_capacity"


@pytest.mark.asyncio
async def test_zero_budget_cannot_be_bypassed_by_native_exhaustion(store, native_fixture):
    import httpx2
    final = await native_job(store, native_fixture(code="usageLimitExceeded"))
    class Child:
        async def start(self, *args, **kwargs): return final
    class Catalog:
        async def models(self, provider): return [{"id":"paid", "capabilities":{"chat":True}, "pricing":{"inputUsdPerToken":1, "outputUsdPerToken":1}, "maxOutputTokens":100}]
    class Vault:
        def get(self, identifier): return "test-only-not-real"
    requests=[]
    def no_network(request):
        requests.append(request)
        raise AssertionError("No API request may be sent")
    router=ProviderRouter(Vault(), store.config(), catalog=Catalog(), transport=httpx2.MockTransport(no_network))
    result=await CouncilRunner(store, Vault(), cli=Child(), router=router).run("Review", limits={"budgetUsd":0})
    assert not requests
    assert result["status"] == "failed"
    assert result["errors"][0]["error"]["code"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_user_time_is_not_reset_when_subscription_would_fallback(store, native_fixture):
    final = await native_job(store, native_fixture(code="usageLimitExceeded"))
    class SlowChild:
        async def start(self, *args, **kwargs):
            await asyncio.sleep(0.03)
            return final
    router = BackupFixture()
    result = await CouncilRunner(store, NoSecrets(), cli=SlowChild(), router=router).run("Review", limits={"timeSeconds":0.005})
    assert not router.calls
    assert result["errors"][0]["error"]["code"] == "time_limit"


@pytest.mark.asyncio
async def test_cancel_and_api_mode_cannot_reuse_capacity_marker(store, native_fixture):
    final=await native_job(store, native_fixture(code="usageLimitExceeded"))
    for mutate in [lambda j:j.update(cancelRequested=True), lambda j:j.update(status="cancelled"),
                   lambda j:j["request"].update(mode="api"), lambda j:j["result"].update(billingMode="api"),
                   lambda j:j["request"].update(permission="workspace-write"), lambda j:j["result"].update(agent="claude"),
                   lambda j:j["result"].update(nativeTurnId="foreign"), lambda j:j["result"].update(usefulOutput=True),
                   lambda j:j["result"]["nativeFailure"].update(provenance="agent_message"),
                   lambda j:j["result"].update(nativeFailure={"code":"partner_verified_rate_limit","provenance":"codex_app_server_account_rate_limits_v2","usedPercent":100,"resetsAt":None}),
                   lambda j:j["request"].pop("transport")]:
        job=json.loads(json.dumps(final)); mutate(job)
        assert verified_partner_failure(job,"codex") is None
    assert verified_partner_failure(final,"claude") is None


@pytest.mark.asyncio
async def test_native_terminal_only_text_and_stderr_are_preserved(store, native_fixture):
    final=await native_job(store,native_fixture(terminal_items=[{"id":"final","type":"agentMessage","text":"Actual final"}],stderr="Native diagnostic"))
    assert final["status"] == "completed"
    assert final["result"]["text"] == "Actual final"
    from pathlib import Path
    stderr_path=next(a["path"] for a in final["result"]["artifacts"] if a["path"].endswith("stderr.txt"))
    assert Path(stderr_path).read_text().strip() == "Native diagnostic"
