"""``approve`` MCP tool (ticket 04).

Approve one or more pending :class:`ApprovalAction` records. All
semantics live in ADR-0009:

- §4.1 single-wait invariant — if the target agent is ``blocked``
  on one of the approved actions, transition it back to
  ``running`` and clear ``waiting_for``. Approving a different
  action leaves the block in place.
- §4.2 all-or-nothing batch — if ANY id is missing or non-pending,
  the entire call errors and NO state changes. The error message
  names every bad id so the moderator can fix one and retry.
- §4.5 — ``note`` (optional) is embedded directly in the writeback
  tag, not sent as a separate cue.

The writeback tag is
``<moderator-approve action-id="a-N" note="...">`` sent to the
agent's tmux session via :class:`TmuxDriver` ``send_keys``.
Driver errors are swallowed: the action is already decided, and
the moderator can re-send via cue if the agent misses it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.lifecycle import transition
from moderator.core.models import (
    AgentState,
    ApprovalAction,
    ModeratorState,
    _utc_now_naive,
)
from moderator.drivers import DriverError, TmuxDriver
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error, make_text_result


TOOL = Tool(
    name="approve",
    description=(
        "Approve one or more pending actions. All-or-nothing batch "
        "(any bad id aborts the whole call). Sends a "
        "<moderator-approve> writeback to each affected agent and "
        "unblocks it if it was waiting on one of the approved ids."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "action_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Action IDs to approve.",
                "minItems": 1,
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


def _validate(
    state: ModeratorState, action_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Return (missing_ids, non_pending_ids). Both empty ⇒ OK."""
    missing = [aid for aid in action_ids if aid not in state.actions]
    non_pending = [
        aid
        for aid in action_ids
        if aid in state.actions and state.actions[aid].status != "pending"
    ]
    return missing, non_pending


def _send_writeback(tmux: TmuxDriver, session: str, aid: str, note: str | None) -> None:
    """Best-effort writeback. DriverError is swallowed."""
    if note:
        tag = f'<moderator-approve action-id="{aid}" note="{note}">'
    else:
        tag = f'<moderator-approve action-id="{aid}">'
    try:
        tmux.send_keys(session, tag)
    except DriverError:
        pass


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    action_ids = arguments.get("action_ids")
    if not isinstance(action_ids, list) or not action_ids:
        return make_error("approve: 'action_ids' must be a non-empty list")

    note = arguments.get("note")
    if note is not None and not isinstance(note, str):
        return make_error("approve: 'note' must be a string when provided")

    state = read_state()
    missing, non_pending = _validate(state, action_ids)
    if missing or non_pending:
        problems = []
        if missing:
            problems.append(f"missing: {missing}")
        if non_pending:
            problems.append(f"non-pending: {non_pending}")
        return make_error(f"approve: {', '.join(problems)} — no changes applied")

    # Apply phase. Build the new actions dict + identify agents to
    # potentially unblock.
    now: datetime = _utc_now_naive()
    affected_agents: dict[str, str] = {}  # agent_name -> the approved aid
    new_actions: dict[str, ApprovalAction] = {}
    for aid in action_ids:
        old = state.actions[aid]
        new = old.model_copy(
            update={
                "status": "approved",
                "decided_at": now,
                "decided_by": "moderator",
            }
        )
        new_actions[aid] = new
        affected_agents[old.agent_name] = aid
    state.actions.update(new_actions)

    # Single-wait unblock per ADR-0009 §4.1. Only the exact aid
    # the agent is waiting on unblocks it.
    for agent_name, approved_aid in affected_agents.items():
        record = state.agents.get(agent_name)
        if record is None:
            continue
        if record.state is AgentState.BLOCKED and record.waiting_for == approved_aid:
            new_record = transition(record, AgentState.RUNNING)
            new_record.waiting_for = None
            state.agents[agent_name] = new_record

    write_state(state)

    # Writeback phase. Driver errors are swallowed.
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    for aid, action in new_actions.items():
        session = action.agent_name
        record = state.agents.get(action.agent_name)
        if record and record.tmux_session:
            session = record.tmux_session
        _send_writeback(tmux, session, aid, note)

    # Human-friendly summary.
    ids = ", ".join(action_ids)
    extra = f" with note {note!r}" if note else ""
    return make_text_result(f"approved: {ids}{extra}")


__all__ = ["TOOL", "handle"]