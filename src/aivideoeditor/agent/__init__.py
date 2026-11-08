"""Claude editing agent (API key or Claude subscription via Claude Code)."""

from .agent import DEFAULT_MODEL, AgentResult, Display, EditAgent
from .claude_code import ClaudeCodeAgent, claude_binary

BACKENDS = ("auto", "claude-code", "api")


def resolve_backend(backend: str | None) -> str:
    """'auto' prefers the Claude subscription (Claude Code) when the CLI is installed."""
    backend = (backend or "auto").lower()
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
    if backend == "auto":
        return "claude-code" if claude_binary() else "api"
    return backend


def make_agent(session, *, backend: str | None = "auto", **kwargs):
    if resolve_backend(backend) == "claude-code":
        return ClaudeCodeAgent(session, **kwargs)
    kwargs.pop("on_tool_result", None)
    return EditAgent(session, **kwargs)


__all__ = ["DEFAULT_MODEL", "AgentResult", "Display", "EditAgent", "ClaudeCodeAgent", "BACKENDS",
           "claude_binary", "make_agent", "resolve_backend"]
