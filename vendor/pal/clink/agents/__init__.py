# Pair adaptation of PAL clink/agents/__init__.py; see THIRD_PARTY_PAL.md.
from pair_core._pal_vendor.models import ResolvedCLIClient
from .base import AgentOutput, BaseCLIAgent, CLIAgentError
from .claude import ClaudeAgent
from .codex import CodexAgent

def create_agent(client: ResolvedCLIClient) -> BaseCLIAgent:
    classes = {"codex": CodexAgent, "claude": ClaudeAgent}
    key = (client.runner or client.name).lower()
    if key not in classes:
        raise ValueError("Pair only supports official Codex and Claude Code CLIs")
    return classes[key](client)
