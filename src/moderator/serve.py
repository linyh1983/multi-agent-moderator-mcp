"""stdio MCP server entry point (ADR-0002).

Run with ``python -m moderator serve`` (or ``moderator serve``). The
server binds its JSON-RPC loop to stdin/stdout — do NOT print to
stdout in handler code, or the framing will corrupt.

Wire-up:

- :func:`list_tools` → :func:`moderator.tools.list_tool_specs`
- :func:`call_tool` → :func:`moderator.tools.call_tool`

The two are thin pass-throughs so all dispatch lives in
``moderator.tools`` and is testable without spawning the stdio loop.
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server

from moderator.tools import call_tool as _dispatch
from moderator.tools import list_tool_specs as _list_tools


SERVER_NAME = "moderator"


def build_server() -> Server:
    """Construct a ``Server`` with our two request handlers wired in."""
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list:  # type: ignore[no-redef]
        return _list_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):  # type: ignore[no-redef]
        return await _dispatch(name, arguments)

    return server


async def run() -> None:
    """Bind stdio and serve until EOF on stdin (client disconnects)."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> int:
    """Synchronous entry used by the CLI dispatch."""
    import asyncio

    asyncio.run(run())
    return 0


__all__ = ["SERVER_NAME", "build_server", "main", "run"]