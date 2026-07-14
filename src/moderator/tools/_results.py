"""Result builder helpers used by every tool module.

Lives in its own leaf module to avoid the circular import that
otherwise forms between ``moderator.tools`` (the package, which
imports each tool) and each tool module (which imports helpers
back from the package). Anything that needs ``mcp.types`` and is
shared by tool handlers belongs here, not in ``__init__.py``.
"""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent


def make_text_result(text: str) -> CallToolResult:
    """Single-text success result."""
    return CallToolResult(content=[TextContent(type="text", text=text)])


def make_error(text: str) -> CallToolResult:
    """Single-text result flagged as an error for the MCP caller."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=True
    )


__all__ = ["make_error", "make_text_result"]