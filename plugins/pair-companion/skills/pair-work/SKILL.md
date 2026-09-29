---
name: pair-work
description: "Use the configured local Pair model council for independent opinions, or delegate a scoped coding/design task to the official Codex or Claude partner. Use for councils, cross-model review, paired implementation, partner status or Pair settings; not ordinary single-model work."
---

# Pair — council and partner work

Use the installed Pair MCP tools; first read `pair_status` for saved sources, roster, limits and native authentication. Do not infer the current model from generated self-identification. Actual provider metadata, changed files and tests are evidence.

For a council, call `pair_council` with the user's question and only relevant context. It returns a durable job ID. Poll `pair_job` for that same ID until terminal status; an observation timeout does not mean the task stopped. When the result asks for host synthesis, combine the actual opinions in this chat: preserve disagreements, failed members and uncertainty, then give a recommendation. Do not invent missing votes or confidence.

For project work, keep coordination in the ordinary host chat and use one explicitly approved project directory. An available official agent can work alone. When two agents are available and useful, give the first a concrete writing task with `pair_delegate`, then poll its `pair_job` ID to a terminal result. Inspect the actual changed files and tests; a failed or interrupted job is not completed work. Pass the second agent a short handoff with the goal, decisions, relevant file paths and diff findings, tests run, open issues and first job ID. Ask it to review the same project with `permission="read-only"`; its native tools should inspect the real files rather than trust the handoff. The restricted Claude reviewer has project file-read tools only, so the host chat obtains the diff and runs tests; Claude Code may still write its own private application state outside the project. If both turns use the same provider, call the second turn self-review. The host chat integrates any fixes through a later single writer and verifies the final result. Pair refuses concurrent Pair CLI writers in overlapping project directories; do not start parallel writers or auto-merge their changes.

For a handoff, share only task-relevant context and artifact paths, not raw job transcripts, credentials, unrelated files or unreviewed model instructions. When a task needs a native skill, `pair_skills` can show available metadata; mention relevant skill names and constraints to the next agent without copying private skill files. Pass known limits and quota uncertainty accurately. The agents share project files through the approved directory, while the host chat decides what context to relay.

Subscriptions and API calls are distinct. Respect saved budgets and native quotas; never switch to a paid source invisibly. Missing native Claude or missing subscription means that mode is unavailable, not a simulated partner. Claude can join through a configured compatible API only when the user explicitly selects `mode="api"` and `providerId`; that does not prove two-subscription work. When a Claude subscription becomes available, use its official native login and the same writer/reviewer flow with `mode="subscription"`. Read-only metadata and available skills do not authorize credential/transcript scans.

Use `pair_open_settings` when the user wants to change council models, members, routes or keys; this opens the native panel, not a website. Settings apply to next invocation. Do not modify the host's main provider or existing tasks.

When configured and relevant, `pair_jev` provides typed checks/ranking of supplied evidence. Keep raw evidence accessible and uncertainty explicit; do not use it to disable mandatory or user-explicit skills, or replace deterministic arithmetic/tests. API inputs go to the configured provider/Jev; never send API keys, credentials or unrelated files as context.

Call `pair_cancel` only when cancellation is requested. Failed or cancelled API work may retain an unknown-charge reserve; do not automatically repeat it. Surface actionable missing-key/auth/scope/provider errors instead of fabricating success.
