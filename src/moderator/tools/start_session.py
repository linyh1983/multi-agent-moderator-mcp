"""``start_session`` MCP tool stub (ticket 01).

Acceptance criterion: against a fake host, returns a clean error string
(no stacktrace) and writes an error-state record to disk afterwards.

Ticket 02 will wire actual SSH / tmux driver behavior.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.models import AgentRecord, AgentState, empty_state
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error


TOOL = Tool(
    name="start_session",
    description=(
        "Start a remote agent session. Stub: ticket 01 only validates "
        "input and records the failure against a fake host. Real driver "
        "(SSH + tmux) lands in ticket 02."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Local handle for this agent (e.g. 'coder-1').",
            },
            "host": {
                "type": "string",
                "description": "Remote host (user@host or ssh alias).",
            },
            "project_dir": {
                "type": "string",
                "description": "Working directory on the remote host.",
            },
            "role_prompt": {
                "type": "string",
                "description": "System prompt establishing the agent's role.",
            },
            "additional_agents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "host": {"type": "string"},
                        "role_prompt": {"type": "string"},
                    },
                    "required": ["name", "host", "role_prompt"],
                    "additionalProperties": False,
                },
                "description": "Peer agents to spawn alongside (stub: ignored).",
            },
        },
        "required": ["name", "host", "project_dir", "role_prompt"],
        "additionalProperties": False,
    },
)


def _record_failure(
    *, name: str, host: str, project_dir: str, reason: str
) -> None:
    """Append a failure record to state. Best-effort; never raises."""
    # If a real state file exists, merge into it so the failure is
    # appended to whatever the moderator already knows about. This
    # matters once tickets 02+ land and start_session can be called
    # against an existing session.
    try:
        state = read_state()
    except Exception:
        # Corrupt / missing / whatever — fall back to empty.
        state = empty_state()

    state.agents[name] = AgentRecord(
        name=name,
        host=host,
        project_dir=project_dir,
        state=AgentState.ERROR,
        error=reason,
    )
    try:
        write_state(state)
    except Exception:
        # Recording failure is itself best-effort. Stub never crashes
        # the caller just because disk is full / locked / whatever.
        pass


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Validate inputs, then emit a clean error (no traceback)."""
    name = arguments.get("name")
    host = arguments.get("host")
    project_dir = arguments.get("project_dir")
    role_prompt = arguments.get("role_prompt")

    if not (name and host and project_dir and role_prompt):
        missing = [
            k
            for k in ("name", "host", "project_dir", "role_prompt")
            if not arguments.get(k)
        ]
        return make_error(f"start_session: missing required args: {missing}")

    reason = (
        f"start_session({name!r}, {host!r}) failed: stub driver cannot "
        f"connect to remote host (ticket 02 implements real SSH)"
    )
    _record_failure(
        name=name,
        host=host,
        project_dir=project_dir,
        reason=reason,
    )
    return make_error(reason)


__all__ = ["TOOL", "handle"]