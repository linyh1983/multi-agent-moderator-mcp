"""``check`` MCP tool stub (ticket 01).

Ticket 01 only requires the empty-state behavior:

    check() against an empty state returns a one-line response
    referencing empty state, e.g. ``(no agents; use start_session
    to begin)``.

Ticket 03 will replace this with a richer snapshot (pending actions,
chat backlog, progress timelines).
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.state.store import read_state
from moderator.tools._results import make_text_result


TOOL = Tool(
    name="check",
    description=(
        "Snapshot of moderator state — agents, pending actions, chat "
        "backlog, progress. Ticket 01 stub: empty-state one-liner only."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "expand_chat": {
                "type": "boolean",
                "description": "If true, include full chat backlog (stub: ignored).",
                "default": False,
            },
            "only_urgent": {
                "type": "boolean",
                "description": "If true, restrict to urgent help requests (stub: ignored).",
                "default": False,
            },
            "max_chat_lines": {
                "type": "integer",
                "description": "Cap on chat lines rendered (stub: ignored).",
                "default": 20,
                "minimum": 1,
            },
        },
        "additionalProperties": False,
    },
)


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Empty-state one-liner; otherwise show a tiny placeholder.

    The placeholder lets ticket 01 callers see "agents exist, but
    rendering hasn't landed yet" rather than a bare empty message.
    """
    del arguments  # unused in ticket 01 stub
    state = read_state()
    if not state.agents:
        return make_text_result("(no agents; use start_session to begin)")
    names = ", ".join(sorted(state.agents.keys()))
    return make_text_result(
        f"agents: {names}  (full rendering lands in ticket 03)"
    )


__all__ = ["TOOL", "handle"]