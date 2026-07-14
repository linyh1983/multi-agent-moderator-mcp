"""``cue`` MCP tool stub (ticket 01)."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.tools._results import make_error


TOOL = Tool(
    name="cue",
    description=(
        "Send a directive (cue) to a target agent or group. Stub: not "
        "implemented in ticket 01 (lands in ticket 04)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Agent name, 'all', or role label.",
            },
            "message": {
                "type": "string",
                "description": "Directive body to send.",
            },
        },
        "required": ["target", "message"],
        "additionalProperties": False,
    },
)


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Placeholder until ticket 04 ships real cue delivery."""
    del arguments
    return make_error("cue: not implemented in ticket 01 (lands in ticket 04)")


__all__ = ["TOOL", "handle"]