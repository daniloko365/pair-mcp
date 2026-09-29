# Pinned PAL CLI reuse

Repository: https://github.com/BeehiveInnovations/pal-mcp-server

Immutable revision: `7afc7c1cc96e23992c8f105f960132c657883bb1`.

License: Apache-2.0, Copyright 2025 Beehive Innovations. Full LICENSE and attribution NOTICE are in `vendor/pal/`. `PROVENANCE.json` contains original source hashes.

## Included scope

Only CLI configuration models, runner/agent subclasses, and JSON/JSONL output parsers. No server, provider registry, preset permissions, install scripts, `.env`, consensus workflow, API clients or PAL conversation memory. Runtime dependency of this subset: standard library plus the project's pinned `pydantic`.

## Explicit adaptations

- Namespaced imports avoid collisions with another PAL install. The root/factory does not load global/user PAL configs and allows only Codex and Claude Code.
- `ResolvedCLIClient.timeout_seconds` allows null (no Pair-imposed deadline).
- Runner gets an explicit sanitized environment instead of ambient environment.
- Full output is streamed through callbacks, retained without PAL's 20k response cap, and preserved on configured timeout/cancellation. Each native CLI has its own process group for scoped termination.
- The upstream parsers and native CLI command construction are reused. Nonzero return codes, Claude `is_error`, permission denials and Codex turn failures must still be checked by Pair; PAL's recovery parser alone is not evidence of successful execution.
- Per-run Pair-only MCP/plugin attenuation prevents inherited Pair tools from recursively creating jobs or making paid calls. It does not alter global client settings or disable unrelated MCP servers. Codex's override parsing was verified natively; Claude's documented deny flags remain fixture-tested until its real CLI is available.

Upstream reputation automation timed out; this is not a security certification. The included source was read before execution. Updates require a new pinned revision, source review, hashes and fixture/real-CLI verification. No upstream installer is executed.

## CLI slice evidence (2026-09-27)

- `.venv/bin/python -m pytest tests/test_cli.py -q`: 21 passed, including detached fixture workers, restart/status/cancel, project symlink rejection, full 35k result, partial timeout evidence, environment allowlisting, split-secret scrubbing and interruption reconciliation. Fixtures make no provider calls.
- Compileall of the owned CLI modules and vendored closure: exit 0.
- Read-only installed Codex diagnostics: `codex-cli 0.158.0-alpha.2`, ChatGPT authentication; official app-server metadata returned quotas, 114 skill metadata records and 7 available models. Repeated after Pair attenuation, still successful. No model turn was started for this check.
- `codex mcp list --json` with child-only Pair override returned exit 0 and Pair disabled. A bare missing-server `enabled=false` override failed transport parsing; the validated disabled sentinel transport avoids that issue.
- Actual subscription task/artifact creation, installed-skill invocation, frozen worker and real Claude execution are acceptance work for the root orchestrator, not certified by these fixtures. Claude CLI/subscription was absent at this check. Native sandbox write scope is not full read/process isolation against a malicious local agent.

## OpenRouter/Claude compatibility follow-up

Official guide: https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration . Pair reuses the configured OpenRouter provider/key through Claude Code's native Messages skin: per-process `ANTHROPIC_BASE_URL=https://openrouter.ai/api`, selected `ANTHROPIC_AUTH_TOKEN`, and explicit empty `ANTHROPIC_API_KEY`. No duplicate account/key, credential import, logout or host configuration change. Only exact official-host providers receive this protocol exception.

The guide documents cached-login conflicts and only guarantees compatibility with Anthropic's first-party provider. Pair reports this limitation rather than clearing credentials. Claude's native `--max-budget-usd` is an estimated-spend control, not a proven custom-provider invoice bound; finite hard API job budgets still fail closed.

Focused suite after this follow-up: 26 passed. New fixtures prove provider reuse, process-only selected-key mapping, no ambient operator keys, result scrubbing, settings unchanged and GUI-PATH local Claude discovery. No paid provider calls were made by these tests. Actual native API generation remains root acceptance work.
