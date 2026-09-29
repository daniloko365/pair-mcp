"""Deterministic local CLI fixtures: no provider calls or credentials."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from pair_core.cli import CliManager, validate_project
from pair_core.pal_bridge import build_client, sanitized_environment
from pair_core.store import Store, ConflictError
from pair_core.cli_worker import SafeStream, run_job


class FixtureVault:
    def get(self, provider_id):
        return "fixture-private-value-only" if provider_id == "test-provider" else None

    def has(self, provider_id):
        return self.get(provider_id) is not None


@pytest.fixture
def store(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / "state")
    cfg = store.config()
    cfg["pair"]["projectRoots"] = [str(project)]
    store.save_config(cfg)
    return store


def test_project_scope_resolves_symlinks_and_rejects_siblings(store, tmp_path):
    project = tmp_path / "project"
    assert validate_project(str(project), store.config()) == project.resolve()
    sibling = tmp_path / "other"
    sibling.mkdir()
    with pytest.raises(ValueError, match="approved"):
        validate_project(str(sibling), store.config())
    alias = project / "escape"
    alias.symlink_to(sibling, target_is_directory=True)
    with pytest.raises(ValueError, match="approved"):
        validate_project(str(alias), store.config())


def test_environment_is_allowlisted_not_secret_pattern_based():
    env = sanitized_environment({"HOME": "/safe", "PATH": "/bin", "OPENAI_API_KEY": "secret", "WHATEVER": "other-secret", "NODE_OPTIONS": "--require attacker", "ANTHROPIC_AUTH_TOKEN": "token", "PYTHONPATH": "/attacker"})
    assert env["HOME"] == "/safe"
    assert env["PATH"] == "/bin"
    assert not {"OPENAI_API_KEY", "WHATEVER", "NODE_OPTIONS", "ANTHROPIC_AUTH_TOKEN", "PYTHONPATH"} & env.keys()


def test_codex_commands_have_native_sandbox_no_bypass_and_null_timeout(tmp_path):
    request = {"agent": "codex", "executable": sys.executable, "project": str(tmp_path), "mode": "subscription", "model": "gpt-6-sol", "effort": "high", "limits": {"timeSeconds": None}, "permission": "workspace-write"}
    client = build_client(request, environment={"PATH": "/bin"})
    assert client.timeout_seconds is None
    assert "workspace-write" in client.config_args
    assert "--dangerously-bypass-approvals-and-sandbox" not in client.config_args
    assert 'model_reasoning_effort="high"' in client.config_args
    assert 'forced_login_method="chatgpt"' in client.config_args
    assert 'mcp_servers.pair={enabled=false,command="pair-disabled-for-child"}' in client.config_args
    assert 'plugins."pair-companion@personal".enabled=false' in client.config_args
    assert not any("mcp_servers.other" in item for item in client.config_args)
    assert client.env == {"PATH": "/bin"}


def test_claude_readonly_and_effort_are_native_flags(tmp_path):
    request = {"agent": "claude", "executable": sys.executable, "project": str(tmp_path), "mode": "subscription", "model": "claude-opus-5-5", "effort": "high", "limits": {}, "permission": "read-only"}
    client = build_client(request, environment={"PATH": "/bin"})
    assert "--effort" in client.config_args
    assert client.config_args[client.config_args.index("--permission-mode") + 1] == "dontAsk"
    assert "--restricted" in client.config_args
    assert "--strict-mcp-config" in client.config_args
    assert "--no-session-persistence" in client.config_args
    assert client.config_args[client.config_args.index("--tools") + 1] == "Read,Glob,Grep"
    assert "plan" not in client.config_args
    assert not any("bypass" in item for item in client.config_args)
    settings = json.loads(client.config_args[client.config_args.index("--settings") + 1])
    assert "mcp__pair__*" in settings["permissions"]["deny"]
    assert "mcp__*" in settings["permissions"]["deny"]
    assert "Bash" in settings["permissions"]["deny"]
    assert "--disallowedTools" in client.config_args


@pytest.mark.asyncio
async def test_finite_api_budget_refused_before_any_worker(store, tmp_path):
    cfg = store.config()
    cfg["providers"] = [{"id": "test-provider", "name": "Fixture", "protocol": "anthropic", "baseUrl": "https://api.example.com", "enabled": True, "manualModels": []}]
    store.save_config(cfg)
    manager = CliManager(store, FixtureVault(), executables={"claude": sys.executable})
    with pytest.raises(ValueError, match="budget"):
        await manager.start("claude", "No paid call", str(tmp_path / "project"), mode="api", providerId="test-provider", limits={"budgetUsd": 1})
    assert store.history() == []


@pytest.mark.asyncio
async def test_missing_cli_is_actionable_without_fake_result(store, tmp_path):
    manager = CliManager(store, FixtureVault(), executables={"claude": None})
    with pytest.raises(ValueError, match="not installed"):
        await manager.start("claude", "Test", str(tmp_path / "project"))
    assert store.history() == []


@pytest.fixture
def fake_cli(tmp_path):
    executable = tmp_path / "fixture-cli"
    executable.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT"); raise SystemExit(0)
if sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})); raise SystemExit(0)
if sys.argv[1:] == ["--version"]:
    print("fixture CLI 1.0"); raise SystemExit(0)
if "app-server" in sys.argv:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request: continue
        values = {
            "initialize": {},
            "account/read": {"account":{"type":"chatgpt","email":"private-email","planType":"plus"}},
            "account/rateLimits/read": {"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":25,"resetsAt":123456,"windowDurationMins":300}}}},
            "skills/list":{"data":[{"skills":[{"name":"fixture-skill","description":"Read-only metadata","path":"/fixture/SKILL.md","enabled":True}]}]},
            "model/list":{"data":[{"model":"gpt-6-sol","displayName":"Sol","supportedReasoningEfforts":[{"reasoningEffort":"high"}]}]},
        }
        print(json.dumps({"id":request["id"],"result":values.get(request["method"],{})}), flush=True)
    raise SystemExit(0)
prompt = sys.stdin.read()
if "WAIT" in prompt:
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"PARTIAL_EVIDENCE"}}), flush=True)
    time.sleep(10)
if "WRITE" in prompt:
    Path("fixture-result.txt").write_text("written by fixture CLI")
if "CLAUDE_ERROR" in prompt:
    print(json.dumps({"type":"result","is_error":True,"result":"native denied edit","permission_denials":[{"tool_name":"Write"}]})); raise SystemExit(0)
text = "x" * 35000 if "LARGE" in prompt else "FIXTURE_OK"
if "LEAK" in prompt:
    key = os.environ.get("PAIR_SELECTED_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
    text = key + " " + str(sorted(os.environ))
if "GATEWAY" in prompt:
    assert os.environ.get("ANTHROPIC_API_KEY") == ""
    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://openrouter.ai/api"
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "fixture-private-value-only"
    assert "OPENROUTER_API_KEY" not in os.environ
    assert "INNOCENT_VARIABLE" not in os.environ
    assert os.environ.get("CLAUDE_CODE_MAX_RETRIES") == "0"
    assert os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "512"
    assert os.environ.get("CLAUDE_CODE_MCP_ALLOWLIST_ENV") == "1"
    text = "GATEWAY_OK " + os.environ["ANTHROPIC_AUTH_TOKEN"]
if "CLAUDE_SUB_ENV" in prompt:
    assert os.environ.get("CLAUDE_CODE_MCP_ALLOWLIST_ENV") == "1"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    text = "CLAUDE_SUB_ENV_OK"
if "FAIL" in prompt:
    print(json.dumps({"type":"error","message":"fixture native error"}), flush=True)
    raise SystemExit(7)
if "--print" in sys.argv:
    print(json.dumps({"type":"result","is_error":False,"result":text,"usage":{"input_tokens":10,"output_tokens":20},"modelUsage":{"anthropic/fixture-model":{}}}))
    raise SystemExit(0)
print(json.dumps({"type":"thread.started","thread_id":"fixture-session"}), flush=True)
if "WRITE" in prompt:
    print(json.dumps({"type":"item.completed","item":{"type":"file_change","changes":[{"path":"fixture-result.txt","kind":"add"}]}}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}), flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}), flush=True)
''', encoding="utf-8")
    executable.chmod(0o700)
    return str(executable)


def prepared_job(store, fake_cli, prompt, *, agent="codex", mode="subscription", limits=None):
    from pair_core.cli import control_disclosure
    cfg = store.config()
    limits = {"maxTokens": None, "timeSeconds": None, "budgetUsd": None} | (limits or {})
    request = {"agent": agent, "executable": fake_cli, "prompt": prompt,
               "project": cfg["pair"]["projectRoots"][0], "projectRoots": cfg["pair"]["projectRoots"],
               "mode": mode, "model": "fixture-model", "effort": "high", "limits": limits,
               "permission": "read-only", "providerId": "test-provider" if mode == "api" else None,
               "provider": {"id":"test-provider","name":"Fixture","protocol":"openai","baseUrl":"https://api.example.com","enabled":True,"manualModels":[]} if mode == "api" else None,
               "controls": control_disclosure(agent, mode, limits)}
    return store.create_job("cli", request)


@pytest.mark.asyncio
async def test_full_output_persisted_without_pal_response_cap(store, fake_cli):
    job = prepared_job(store, fake_cli, "LARGE")
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "completed"
    assert final["result"]["text"] == "x" * 35000
    assert final["result"]["model"] is None  # selected ID is not identity evidence
    assert len(Path(final["result"]["artifacts"][-1]["path"]).read_text()) > 35000
    assert Store(store.state_dir).job(job["id"])["result"]["text"] == "x" * 35000


@pytest.mark.asyncio
async def test_native_errors_do_not_become_completed(store, fake_cli):
    job = prepared_job(store, fake_cli, "FAIL")
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "failed"
    assert "fixture native error" in (store.state_dir / "jobs" / job["id"] / "stdout.txt").read_text()


@pytest.mark.asyncio
async def test_claude_permission_denial_is_failure(store, fake_cli):
    job = prepared_job(store, fake_cli, "CLAUDE_ERROR", agent="claude")
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "failed"
    assert "native denied edit" == final["result"]["text"]


@pytest.mark.asyncio
async def test_api_receives_only_selected_key_and_output_scrubbed(store, fake_cli, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "operator-secret-not-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-secret-not-forwarded")
    monkeypatch.setenv("INNOCENT_VARIABLE", "also-secret-not-forwarded")
    job = prepared_job(store, fake_cli, "LEAK", mode="api")
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "completed"
    assert "[REDACTED]" in final["result"]["text"]
    assert "OPENROUTER_API_KEY" not in final["result"]["text"]
    assert "ANTHROPIC_API_KEY" not in final["result"]["text"]
    assert "INNOCENT_VARIABLE" not in final["result"]["text"]
    assert final["costUsd"] is None
    for path in (store.state_dir / "jobs" / job["id"]).iterdir():
        assert "fixture-private-value-only" not in path.read_text()


def test_split_key_and_unicode_survive_safe_stream(tmp_path):
    path = tmp_path / "safe.txt"
    stream = SafeStream(path, ["fixture-private-value-only"])
    stream.feed(b"prefix fixture-private-")
    assert path.read_text() == ""
    stream.feed("value-only привет\n".encode())
    stream.close()
    assert path.read_text() == "prefix [REDACTED] привет\n"


@pytest.mark.asyncio
async def test_user_time_limit_retains_partial_output(store, fake_cli):
    job = prepared_job(store, fake_cli, "WAIT", limits={"timeSeconds": 0.12})
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "timed_out"
    assert "PARTIAL_EVIDENCE" in (store.state_dir / "jobs" / job["id"] / "stdout.txt").read_text()


@pytest.mark.asyncio
async def test_cancel_uses_owned_worker_flag_preserves_partial_output(store, fake_cli):
    job = prepared_job(store, fake_cli, "WAIT")
    task = asyncio.create_task(run_job(store, FixtureVault(), job["id"]))
    for _ in range(100):
        if store.job(job["id"]).get("nativePid"):
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.04)
    await CliManager(store, FixtureVault()).cancel(job["id"])
    final = await asyncio.wait_for(task, 3)
    assert final["status"] == "cancelled"
    assert "PARTIAL_EVIDENCE" in (store.state_dir / "jobs" / job["id"] / "stdout.txt").read_text()


@pytest.mark.asyncio
async def test_documented_status_metadata_no_email_or_credentials(store, fake_cli):
    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli, "claude": None})
    status = await manager.status()
    assert status["codex"]["billingMode"] == "subscription"
    assert status["codex"]["quotas"][0]["remainingPercent"] == 75
    assert status["codex"]["skills"][0]["name"] == "fixture-skill"
    assert status["codex"]["models"][0]["id"] == "gpt-6-sol"
    assert status["claude"]["installed"] is False
    assert "private-email" not in json.dumps(status)


@pytest.mark.asyncio
async def test_start_snapshots_limits_and_project_without_worker_keys(store, fake_cli):
    class Process:
        pid = 123456
    launches = []
    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli}, worker_launcher=lambda job: launches.append(job) or Process())
    job = await manager.start("codex", "WRITE", store.config()["pair"]["projectRoots"][0], limits={"budgetUsd":0,"maxTokens":32}, permission="workspace-write")
    assert job["request"]["controls"]["maxTokensEnforced"] is False
    cfg = store.config()
    cfg["pair"]["projectRoots"] = []
    store.save_config(cfg)
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "completed"
    assert Path(job["request"]["project"], "fixture-result.txt").read_text() == "written by fixture CLI"
    assert any(item["type"] == "project_file" for item in final["result"]["artifacts"])


@pytest.mark.asyncio
async def test_actual_detached_fixture_worker_survives_manager_recreation(store, fake_cli):
    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli})
    job = await manager.start("codex", "WRITE", store.config()["pair"]["projectRoots"][0])
    for _ in range(150):
        latest = Store(store.state_dir).job(job["id"])
        if latest["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.02)
    assert latest["status"] == "completed", latest.get("error")
    assert latest["result"]["text"] == "FIXTURE_OK"
    assert latest["result"]["nativeSessionId"] == "fixture-session"
    assert Path(latest["request"]["project"], "fixture-result.txt").is_file()


@pytest.mark.asyncio
async def test_detached_cancel_from_new_manager_is_durable(store, fake_cli):
    job = await CliManager(store, FixtureVault(), executables={"codex": fake_cli}).start("codex", "WAIT", store.config()["pair"]["projectRoots"][0])
    for _ in range(150):
        latest = Store(store.state_dir).job(job["id"])
        if latest.get("nativePid"):
            break
        await asyncio.sleep(0.02)
    assert latest.get("nativePid")
    await CliManager(Store(store.state_dir), FixtureVault()).cancel(job["id"])
    for _ in range(150):
        latest = Store(store.state_dir).job(job["id"])
        if latest["status"] in {"cancelled", "failed"}:
            break
        await asyncio.sleep(0.02)
    assert latest["status"] == "cancelled", latest.get("error")


@pytest.mark.asyncio
async def test_nonfinite_limits_cannot_disable_runtime_controls(store, fake_cli):
    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli})
    with pytest.raises(ValueError, match="finite"):
        await manager.start("codex", "No model call", store.config()["pair"]["projectRoots"][0], limits={"timeSeconds": float("inf")})
    assert store.history() == []


def test_reconciliation_preserves_unknown_api_charge_no_pid_kill(store, fake_cli, monkeypatch):
    job = prepared_job(store, fake_cli, "Never execute", mode="api")
    store.update_job(job["id"], status="running", pid=99999999)
    directory = store.state_dir / "jobs" / job["id"]
    directory.mkdir(parents=True)
    (directory / "stdout.txt").write_text("sanitized partial evidence")
    calls = []
    def absent(pid, signal):
        calls.append((pid, signal))
        raise ProcessLookupError()
    monkeypatch.setattr(os, "kill", absent)
    CliManager(store, FixtureVault()).reconcile()
    latest = store.job(job["id"])
    assert latest["status"] == "interrupted"
    assert latest["costUsd"] is None and latest["reservedUsd"] is None
    assert latest["result"]["artifacts"][0]["path"].endswith("stdout.txt")
    assert calls == [(99999999, 0)]


def test_reconciliation_does_not_guess_queued_job_from_age(store, fake_cli):
    job = prepared_job(store, fake_cli, "Never execute")
    store.update_job(job["id"], createdAt=0)
    CliManager(store, FixtureVault()).reconcile()
    assert store.job(job["id"])["status"] == "queued"


def test_reconciliation_releases_writer_when_launcher_is_gone(store, fake_cli, monkeypatch):
    project = store.config()["pair"]["projectRoots"][0]
    job = store.create_job("cli", {"project": project, "permission": "workspace-write", "mode": "api"})
    store.update_job(job["id"], launcherPid=99999999)
    calls = []

    def absent(pid, signal):
        calls.append((pid, signal))
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", absent)
    CliManager(store, FixtureVault()).reconcile()
    final = store.job(job["id"])
    assert final["status"] == "interrupted"
    assert final["pid"] is None
    assert final["costUsd"] is None and final["reservedUsd"] is None
    assert calls == [(99999999, 0)]
    assert store.create_job("cli", {"project": project, "permission": "workspace-write"})["status"] == "queued"


@pytest.mark.asyncio
async def test_cancel_queued_writer_without_pid_releases_reservation(store):
    project = store.config()["pair"]["projectRoots"][0]
    job = store.create_job("cli", {"project": project, "permission": "workspace-write", "mode": "api"})
    final = await CliManager(store, FixtureVault()).cancel(job["id"])
    assert final["status"] == "cancelled"
    assert final["pid"] is None
    assert final["costUsd"] is None and final["reservedUsd"] is None
    with pytest.raises(ConflictError):
        store.update_job(job["id"], status="running", pid=os.getpid())
    assert store.create_job("cli", {"project": project, "permission": "workspace-write"})["status"] == "queued"


@pytest.mark.asyncio
async def test_queued_event_failure_releases_writer_without_launch(store, fake_cli, monkeypatch):
    project = store.config()["pair"]["projectRoots"][0]
    launched = []
    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli},
                         worker_launcher=lambda job: launched.append(job))
    original_event = store.event

    def fail_queued_event(job_id, kind, data):
        if kind == "queued":
            raise OSError("fixture event write failure")
        return original_event(job_id, kind, data)

    monkeypatch.setattr(store, "event", fail_queued_event)
    with pytest.raises(RuntimeError, match="could not start"):
        await manager.start("codex", "Never execute", project, permission="workspace-write")
    assert launched == []
    first = store.history()[0]
    assert first["status"] == "failed" and first["pid"] is None
    assert store.create_job("cli", {"project": project, "permission": "workspace-write"})["status"] == "queued"


@pytest.mark.asyncio
async def test_failed_worker_spawn_releases_writer(store, fake_cli):
    project = store.config()["pair"]["projectRoots"][0]

    def cannot_spawn(job):
        raise OSError("fixture launcher unavailable")

    manager = CliManager(store, FixtureVault(), executables={"codex": fake_cli}, worker_launcher=cannot_spawn)
    with pytest.raises(RuntimeError, match="could not start"):
        await manager.start("codex", "Never execute", project, permission="workspace-write")
    first = store.history()[0]
    assert first["status"] == "failed" and first["pid"] is None
    assert store.create_job("cli", {"project": project, "permission": "workspace-write"})["status"] == "queued"


def test_reconciliation_detects_reused_pid_without_signalling_it(store, fake_cli, monkeypatch):
    job = prepared_job(store, fake_cli, "Never execute")
    store.update_job(job["id"], status="running", pid=os.getpid())
    calls = []
    monkeypatch.setattr(os, "kill", lambda pid, signal: calls.append((pid, signal)))
    CliManager(store, FixtureVault()).reconcile()
    assert store.job(job["id"])["status"] == "interrupted"
    assert calls == [(os.getpid(), 0)]


@pytest.mark.asyncio
async def test_openrouter_api_reuses_existing_openai_provider_for_claude(store, fake_cli):
    cfg = store.config()
    cfg["providers"] = [{"id":"test-provider","name":"OpenRouter","protocol":"openai","baseUrl":"https://openrouter.ai/api/v1","enabled":True,"manualModels":[]}]
    store.save_config(cfg)
    class Process:
        pid = 123456
    manager = CliManager(store, FixtureVault(), executables={"claude": fake_cli}, worker_launcher=lambda job: Process())
    before = store.config()
    job = await manager.start("claude", "No paid call", cfg["pair"]["projectRoots"][0], mode="api", providerId="test-provider", model="anthropic/claude-opus-5.5", limits={"budgetUsd":None})
    assert job["request"]["providerId"] == "test-provider"
    assert store.config() == before
    assert "fixture-private-value-only" not in json.dumps(job)


def test_local_claude_fallback_without_shell_path(tmp_path, fake_cli, monkeypatch):
    from pair_core.cli import discover_executable
    monkeypatch.setattr("pair_core.cli.shutil.which", lambda agent: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "claude").symlink_to(fake_cli)
    assert discover_executable("claude") == str(Path(fake_cli).resolve())


def test_openrouter_gateway_environment_only_selected_key():
    from pair_core.pal_bridge import selected_api_environment
    provider = {"protocol":"openai", "baseUrl":"https://openrouter.ai/api/v1"}
    env = selected_api_environment("claude", provider, "fixture-selected-key")
    assert env == {"ANTHROPIC_AUTH_TOKEN":"fixture-selected-key", "ANTHROPIC_API_KEY":"", "ANTHROPIC_BASE_URL":"https://openrouter.ai/api"}
    direct = selected_api_environment("claude", {"protocol":"anthropic", "baseUrl":"https://api.anthropic.com"}, "fixture-direct-key")
    assert direct == {"ANTHROPIC_API_KEY":"fixture-direct-key", "ANTHROPIC_BASE_URL":"https://api.anthropic.com"}
    with pytest.raises(ValueError):
        selected_api_environment("claude", {"protocol":"openai", "baseUrl":"https://openrouter.ai.attacker.example/api"}, "fixture-selected-key")


@pytest.mark.asyncio
async def test_openrouter_claude_worker_selected_key_scrubbed_no_host_changes(store, fake_cli, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "operator-secret-not-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-secret-not-forwarded")
    monkeypatch.setenv("INNOCENT_VARIABLE", "unknown-secret-not-forwarded")
    cfg = store.config()
    cfg["providers"] = [{"id":"test-provider","name":"OpenRouter","protocol":"openai","baseUrl":"https://openrouter.ai/api/v1","enabled":True,"manualModels":[]}]
    store.save_config(cfg)
    class Process:
        pid = 123456
    manager = CliManager(store, FixtureVault(), executables={"claude":fake_cli}, worker_launcher=lambda job: Process())
    before = store.config()
    job = await manager.start("claude", "GATEWAY", cfg["pair"]["projectRoots"][0], mode="api", providerId="test-provider", model="anthropic/fixture-model", limits={"budgetUsd":None,"maxTokens":512}, permission="read-only")
    final = await run_job(store, FixtureVault(), job["id"])
    assert final["status"] == "completed", final.get("error")
    assert final["result"]["billingMode"] == "api"
    assert final["result"]["providerId"] == "test-provider"
    assert final["result"]["text"] == "GATEWAY_OK [REDACTED]"
    assert store.config() == before
    for path in (store.state_dir / "jobs" / job["id"]).iterdir():
        assert "fixture-private-value-only" not in path.read_text()


def test_native_estimate_disclosed_but_not_claimed_hard_invoice_cap():
    from pair_core.cli import control_disclosure
    disclosure = control_disclosure("claude", "api", {"budgetUsd":1})
    assert disclosure["nativeEstimatedApiBudgetSupported"] is True
    assert disclosure["nativeEstimatedApiBudgetActive"] is False
    assert disclosure["apiBudgetEnforced"] is False
    assert any("estimated-spend" in warning for warning in disclosure["warnings"])


def test_explicit_native_estimate_and_no_tools_are_scoped_flags(tmp_path):
    request = {"agent":"claude","executable":sys.executable,"project":str(tmp_path),"mode":"api","model":"anthropic/fixture-model","effort":"high","limits":{},"permission":"read-only","nativeEstimatedBudgetUsd":0.25,"tools":[]}
    client = build_client(request, environment={"PATH":"/bin"})
    assert client.config_args[client.config_args.index("--max-budget-usd") + 1] == "0.25"
    assert client.config_args[client.config_args.index("--tools") + 1] == ""
    assert "--disallowedTools" in client.config_args


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Write", "Edit", "Bash"])
async def test_claude_readonly_rejects_write_tools_before_worker(store, fake_cli, tool):
    manager = CliManager(store, FixtureVault(), executables={"claude": fake_cli})
    with pytest.raises(ValueError, match="file-read tools"):
        await manager.start("claude", "Never execute", store.config()["pair"]["projectRoots"][0],
                            permission="read-only", tools=[tool])
    assert store.history() == []


@pytest.mark.asyncio
async def test_estimated_cap_does_not_bypass_finite_hard_budget_rule(store, fake_cli):
    manager = CliManager(store, FixtureVault(), executables={"claude":fake_cli})
    with pytest.raises(ValueError, match="hard API budget"):
        await manager.start("claude", "No call", store.config()["pair"]["projectRoots"][0], mode="api", nativeEstimatedBudgetUsd=0.25, limits={"budgetUsd":1})
    assert store.history() == []


def test_claude_explicit_workspace_write_maps_native_edits_without_bypass(tmp_path):
    request = {"agent":"claude","executable":sys.executable,"project":str(tmp_path),"mode":"api","model":"anthropic/fixture-model","effort":"low","limits":{},"permission":"workspace-write","tools":["Write"]}
    client = build_client(request, environment={"PATH":"/bin"})
    assert client.config_args[client.config_args.index("--permission-mode") + 1] == "acceptEdits"
    settings = json.loads(client.config_args[client.config_args.index("--settings") + 1])
    assert settings["permissions"]["defaultMode"] == "acceptEdits"
    assert settings["permissions"]["additionalDirectories"] == []
    assert client.working_dir == tmp_path
    assert not any("bypass" in value or "skip-permissions" in value for value in client.config_args)


@pytest.mark.asyncio
async def test_claude_subscription_sets_native_mcp_env_attenuation(store, fake_cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "operator-secret-not-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-secret-not-forwarded")
    job=prepared_job(store,fake_cli,"CLAUDE_SUB_ENV",agent="claude")
    final=await run_job(store,FixtureVault(),job["id"])
    assert final["status"]=="completed"
    assert final["result"]["text"]=="CLAUDE_SUB_ENV_OK"
