"""CLI dispatch for the `moderator` and `moderator-mcp` console scripts.

Subcommands:
- serve          Start the stdio MCP server (Claude Code spawns this).
- state-inspect  Print / dump the state file.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moderator",
        description="Moderator MCP Server CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "serve",
        help="Start the stdio MCP server (used by Claude Code as a subprocess).",
    )

    inspect = sub.add_parser(
        "state-inspect",
        help="Print or dump the state file.",
    )
    inspect.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="json",
        help="Output format (default: json).",
    )
    inspect.add_argument(
        "--agent",
        default=None,
        help="Filter to entries related to a specific agent name.",
    )
    inspect.add_argument(
        "--since",
        default=None,
        metavar="ISO-8601",
        help="Lower-bound timestamp for entries.",
    )
    inspect.add_argument(
        "--until",
        default=None,
        metavar="ISO-8601",
        help="Upper-bound timestamp for entries.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch subcommand. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from moderator.serve import main as run_serve

        return run_serve()

    if args.command == "state-inspect":
        from moderator.state.inspect_cmd import run as run_inspect

        return run_inspect(
            output_format=args.format,
            agent=args.agent,
            since=args.since,
            until=args.until,
        )

    parser.error(f"unknown command: {args.command!r}")
    return 2  # unreachable: required=True guarantees a subcommand


if __name__ == "__main__":
    sys.exit(main())
