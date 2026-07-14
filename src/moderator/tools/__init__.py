"""MCP tool registry and dispatcher.

Each tool module exposes:

- ``TOOL``: an ``mcp.types.Tool`` with name, description, input schema.
- ``handle(arguments)``: an async callable that returns
  ``mcp.types.CallToolResult``.

The dispatcher in :func:`list_tool_specs` and :func:`call_tool` is the
only thing ``serve.py`` needs to know — see ADR-0002.

Ticket 01 ships these as stubs (full behavior lands in tickets 02–08).
``check`` and ``start_session`` have meaningful stub semantics; the
other four return "not implemented yet" with the relevant ticket #.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

from moderator.tools import (  # noqa: E402
    approve,
    check,
    cue,
    reject,
    start_session,
    stop_session,
)
from moderator.tools._results import make_error, make_text_result  # noqa: E402


ToolHandler = Callable[[dict[str, Any]], Awaitable[CallToolResult]]


# Name → (Tool, handler) for direct lookup. Each value is the (definition,
# handler) pair so adding/removing tools is a one-line change here.
_REGISTRY: dict[str, tuple[Tool, ToolHandler]] = {
    tool.name: (tool, handler)
    for tool, handler in (
        (start_session.TOOL, start_session.handle),
        (check.TOOL, check.handle),
        (approve.TOOL, approve.handle),
        (reject.TOOL, reject.handle),
        (cue.TOOL, cue.handle),
        (stop_session.TOOL, stop_session.handle),
    )
}


def list_tool_specs() -> list[Tool]:
    """Tool definitions, ordered for stable UI."""
    order = [
        "start_session",
        "check",
        "approve",
        "reject",
        "cue",
        "stop_session",
    ]
    return [_REGISTRY[name][0] for name in order]


async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Dispatch a tool call by name. Unknown names return isError=True."""
    entry = _REGISTRY.get(name)
    if entry is None:
        return CallToolResult(
            content=[TextContent(type="text", text=f"unknown tool: {name!r}")],
            isError=True,
        )
    _, handler = entry
    return await handler(arguments)


__all__ = [
    "ToolHandler",
    "call_tool",
    "list_tool_specs",
    "make_error",
    "make_text_result",
]
