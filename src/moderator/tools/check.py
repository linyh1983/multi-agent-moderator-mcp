"""``check`` MCP tool (ticket 03 — real rendering).

Renders a global snapshot in four sections, in this priority order:

1. ``## Urgent help``   — every :class:`HelpRequest` for any agent
2. ``## Pending approvals`` — :class:`ApprovalAction` records with
   ``status == "pending"``
3. ``## Agents``        — one line per agent: name, host, state,
   last-output age, progress count + tail (last 1-3 entries)
4. ``## Recent chat``   — tail of ``state.chat``, capped at
   ``max_chat_lines``

Agents in ``stuck`` / ``offline`` / ``error`` are visually flagged
with a leading ``⚠`` glyph in their row. The text-based UI is
explicitly accepted by ADR-0008 ("consequences: v1 UI is text
identifiers").

External-change detection (ADR-0011): the tool remembers the
``mtime`` of the state file it last read in this process. If the
file's ``mtime`` differs on the next call, a warning line is
prepended to the output. The first call in a process has no
baseline and never warns.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.models import (
    AgentRecord,
    AgentState,
    ModeratorState,
    ProgressEntry,
)
from moderator.state.store import (
    STATE_PATH_ENV,
    default_state_path,
    read_state,
)
from moderator.tools._results import make_text_result


TOOL = Tool(
    name="check",
    description=(
        "Snapshot of moderator state — urgent help, pending approvals, "
        "agents (state + last-output age + progress count + tail), and "
        "recent chat. Agents in stuck/offline/error are visually "
        "flagged. Warns on the first call after an external state "
        "file write (other moderator session?)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "only_urgent": {
                "type": "boolean",
                "description": "If true, render only the urgent help section.",
                "default": False,
            },
            "max_chat_lines": {
                "type": "integer",
                "description": "Cap on chat lines rendered.",
                "default": 20,
                "minimum": 1,
            },
        },
        "additionalProperties": False,
    },
)


_PROGRESS_TAIL_LIMIT = 3
_DEFAULT_CHAT_LINES = 20
_FLAG_STATES = frozenset({AgentState.STUCK, AgentState.OFFLINE, AgentState.ERROR})


# ---------------------------------------------------------------------------
# mtime cache — process-local "last seen state file mtime" (ADR-0011)
# ---------------------------------------------------------------------------
#
# Per ADR-0011 §decision the moderator process keeps a memory view of
# the state file. On each ``check`` we compare the current mtime to
# the value we saw on the previous read. Mismatch ⇒ warn. The cache
# is process-local: tests reset it via :func:`reset_mtime_cache`.

_last_mtime: float | None = None


def reset_mtime_cache() -> None:
    """Drop the cached mtime. Test-only API."""
    global _last_mtime
    _last_mtime = None


def _current_mtime(path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _maybe_external_change_warning(path) -> str | None:
    """Return a warning line if the state file's mtime has changed
    since this process last read it. Updates the cache."""
    global _last_mtime
    current = _current_mtime(path)
    warning: str | None = None
    if _last_mtime is not None and current is not None and current != _last_mtime:
        warning = (
            "⚠️  State changed externally since you last read it "
            "(other moderator session?).\n"
            f"mtime: {_last_mtime:.3f} → {current:.3f}"
        )
    _last_mtime = current
    return warning


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _format_age(ts: datetime | None, *, now: datetime | None = None) -> str:
    if ts is None:
        return "never"
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now - ts
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h{minutes % 60}m"
    days = hours // 24
    return f"{days}d{hours % 24}h"


def _render_help_section(state: ModeratorState) -> str:
    if not state.help_requests:
        return ""
    lines = ["## Urgent help", ""]
    for req in state.help_requests:
        lines.append(
            f"  - {req.agent_name}: {req.content}  ({_format_age(req.ts)})"
        )
    lines.append("")
    return "\n".join(lines)


def _render_pending_section(state: ModeratorState) -> str:
    pending = [a for a in state.actions.values() if a.status == "pending"]
    if not pending:
        return ""
    lines = ["## Pending approvals", ""]
    for action in sorted(pending, key=lambda a: a.seq):
        lines.append(
            f"  - [{action.id}] {action.agent_name}: {action.content}  "
            f"({_format_age(action.created_at)})"
        )
    lines.append("")
    return "\n".join(lines)


def _format_progress_tail(entries: list[ProgressEntry]) -> str:
    if not entries:
        return "0 progress"
    tail = entries[-_PROGRESS_TAIL_LIMIT:]
    rendered = " | ".join(e.text for e in tail)
    return f"{len(entries)} progress: {rendered}"


def _render_agent_row(record: AgentRecord) -> str:
    flag = "⚠ " if record.state in _FLAG_STATES else "  "
    parts = [
        f"{flag}{record.name}",
        f"({record.host})",
        f"state={record.state.value}",
        f"last_output={_format_age(record.last_output_at)}",
    ]
    return " ".join(parts)


def _render_agents_section(
    state: ModeratorState, progress: dict[str, list[ProgressEntry]]
) -> str:
    if not state.agents:
        return "## Agents\n\n  (no agents; use start_session to begin)\n"
    lines = ["## Agents", ""]
    for name, rec in state.agents.items():
        row = _render_agent_row(rec)
        tail = _format_progress_tail(progress.get(name, []))
        lines.append(f"  {row}")
        lines.append(f"      {tail}")
    lines.append("")
    return "\n".join(lines)


def _render_chat_section(
    state: ModeratorState, max_lines: int
) -> str:
    if not state.chat:
        return "## Recent chat (tail)\n\n  (none)\n"
    tail = state.chat[-max_lines:]
    lines = ["## Recent chat (tail)", ""]
    for msg in tail:
        sender = msg.from_agent or "system"
        target = msg.to_agent or "all"
        # ADR-0009 §4.6 reject privacy: a chat entry tagged with
        # ``private_to_originator`` carries a moderator-only
        # message (typically a reject reason). Render it as a
        # redacted placeholder so peer Agents reading the chat
        # tail don't see the body.
        body = msg.text
        meta = msg.metadata or {}
        private_to = meta.get("private_to_originator")
        if private_to:
            body = f"[private: see agent {private_to}]"
        lines.append(
            f"  - {_format_age(msg.ts)} {sender}→{target} "
            f"({msg.kind}): {body}"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    only_urgent: bool = bool(arguments.get("only_urgent", False))
    max_chat_lines: int = int(arguments.get("max_chat_lines", _DEFAULT_CHAT_LINES))

    # Resolve the state path BEFORE reading so the mtime we record is
    # the one we just observed.
    override = os.environ.get(STATE_PATH_ENV)
    state_path = Path(override) if override else default_state_path()
    state = read_state(state_path)

    warning = _maybe_external_change_warning(state_path)

    if only_urgent:
        body = _render_help_section(state)
    else:
        body = "\n".join(
            filter(
                None,
                [
                    _render_help_section(state),
                    _render_pending_section(state),
                    _render_agents_section(state, state.progress),
                    _render_chat_section(state, max_chat_lines),
                ],
            )
        )

    if not body:
        body = "(no agents; use start_session to begin)"

    text = (warning + "\n\n" + body) if warning else body
    return make_text_result(text)


__all__ = ["TOOL", "handle", "reset_mtime_cache"]
