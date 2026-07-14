"""``stop_session`` MCP tool stub (ticket 01)."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.tools._results import make_error


TOOL = Tool(
    name="stop_session",
    description=(
        "Stop one or all agents and write a postmortem record. "
        "Stub: not implemented in ticket 01 (lands in ticket 02)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Agent to stop. Omit to stop all.",
            },
            "force": {
                "type": "boolean",
                "description": "If true, skip the graceful-shutdown wait.",
                "default": False,
            },
            "grace_seconds": {
                "type": "integer",
                "description": "Seconds to wait before SIGKILL.",
                "minimum": 0,
            },
        },
        "additionalProperties": False,
    },
)


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Placeholder until ticket 02 ships real shutdown."""
    del arguments
    return make_error(
        "stop_session: not implemented in ticket 01 (lands in ticket 02)"
    )


__all__ = ["TOOL", "handle"]