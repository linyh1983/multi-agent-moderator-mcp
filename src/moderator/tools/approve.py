"""``approve`` MCP tool stub (ticket 01)."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.tools._results import make_error


TOOL = Tool(
    name="approve",
    description=(
        "Approve one or more pending actions. Stub: not implemented in "
        "ticket 01 (lands in ticket 05)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "action_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Action IDs to approve.",
            },
            "note": {
                "type": "string",
                "description": "Optional moderator note attached to the approval.",
            },
        },
        "required": ["action_ids"],
        "additionalProperties": False,
    },
)


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Placeholder until ticket 05 ships real approval semantics."""
    del arguments
    return make_error(
        "approve: not implemented in ticket 01 (lands in ticket 05)"
    )


__all__ = ["TOOL", "handle"]