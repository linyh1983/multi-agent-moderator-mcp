"""``reject`` MCP tool stub (ticket 01)."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.tools._results import make_error


TOOL = Tool(
    name="reject",
    description=(
        "Reject one or more pending actions with a reason. Stub: not "
        "implemented in ticket 01 (lands in ticket 05)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "action_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Action IDs to reject.",
            },
            "reason": {
                "type": "string",
                "description": "Why these actions are being rejected.",
            },
        },
        "required": ["action_ids", "reason"],
        "additionalProperties": False,
    },
)


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Placeholder until ticket 05 ships real rejection semantics."""
    del arguments
    return make_error(
        "reject: not implemented in ticket 01 (lands in ticket 05)"
    )


__all__ = ["TOOL", "handle"]