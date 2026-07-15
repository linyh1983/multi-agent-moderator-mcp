"""``reject`` MCP tool (ticket 04).

Reject one or more pending :class:`ApprovalAction` records with a
mandatory ``reason``. All semantics live in ADR-0009:

- §4.1 single-wait invariant — if the target agent is ``blocked``
  on one of the rejected actions, transition it back to
  ``running`` and clear ``waiting_for``. Rejecting a different
  action leaves the block in place.
- §4.2 all-or-nothing batch — if ANY id is missing or non-pending,
  the entire call errors and NO state changes.
- §4.3 — reject does NOT auto-create a new pending action. If the
  moderator wants a counter-proposal, they call ``cue`` separately.
- §4.6 reject privacy — the full ``reason`` is stored in
  ``state.actions[id].reject_reason`` (always visible to the
  moderator) and ALSO copied into ``state.chat`` with a
  ``private_to_originator`` metadata flag pointing at the
  originating agent. The ``check`` tool renders the chat entry as
  ``[private: see agent X]`` so peers do not see the reason.

The writeback tag is
``<moderator-reject action-id="a-N" reason="...">`` sent to the
agent's tmux session via :class:`TmuxDriver` ``send_keys``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.lifecycle import transition
from moderator.core.models import (
    AgentState,
    ApprovalAction,
    ChatMessage,
    ModeratorState,
    _utc_now_naive,
)
from moderator.drivers import DriverError, TmuxDriver
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error, make_text_result


TOOL = Tool(
    name="reject",
    description=(
        "Reject one or more pending actions with a reason. "
        "All-or-nothing batch. Reason is mandatory. Sends a "
        "<moderator-reject> writeback to each affected agent. "
        "Reject does NOT auto-create a new pending action."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "action_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Action IDs to reject.",
                "minItems": 1,
            },
            "reason": {
                "type": "string",
                "description": "Why these actions are being rejected (mandatory).",
            },
        },
        "required": ["action_ids", "reason"],
        "additionalProperties": False,
    },
)


def _validate(
    state: ModeratorState, action_ids: list[str]
) -> tuple[list[str], list[str]]:
    missing = [aid for aid in action_ids if aid not in state.actions]
    non_pending = [
        aid
        for aid in action_ids
        if aid in state.actions and state.actions[aid].status != "pending"
    ]
    return missing, non_pending


def _send_writeback(tmux: TmuxDriver, session: str, aid: str, reason: str) -> None:
    tag = f'<moderator-reject action-id="{aid}" reason="{reason}">'
    try:
        tmux.send_keys(session, tag)
    except DriverError:
        pass


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    action_ids = arguments.get("action_ids")
    if not isinstance(action_ids, list) or not action_ids:
        return make_error("reject: 'action_ids' must be a non-empty list")

    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return make_error("reject: 'reason' is mandatory and must be a non-empty string")

    state = read_state()
    missing, non_pending = _validate(state, action_ids)
    if missing or non_pending:
        problems = []
        if missing:
            problems.append(f"missing: {missing}")
        if non_pending:
            problems.append(f"non-pending: {non_pending}")
        return make_error(f"reject: {', '.join(problems)} — no changes applied")

    now: datetime = _utc_now_naive()
    affected_agents: dict[str, str] = {}
    new_actions: dict[str, ApprovalAction] = {}
    for aid in action_ids:
        old = state.actions[aid]
        new = old.model_copy(
            update={
                "status": "rejected",
                "decided_at": now,
                "decided_by": "moderator",
                "reject_reason": reason,
            }
        )
        new_actions[aid] = new
        affected_agents[old.agent_name] = aid
    state.actions.update(new_actions)

    # Append one private chat entry per rejected action
    # (ADR-0009 §4.6). The reason text is in the entry body so a
    # moderator reading raw chat sees it; peers reading via
    # ``check`` will see it rendered as ``[private: see agent X]``.
    for aid, action in new_actions.items():
        state.chat.append(
            ChatMessage(
                id=f"msg-reject-{aid}",
                ts=now,
                kind="system",
                from_agent="moderator",
                to_agent=action.agent_name,
                text=reason,
                metadata={"private_to_originator": action.agent_name, "action_id": aid},
            )
        )

    # Single-wait unblock per ADR-0009 §4.1.
    for agent_name, approved_aid in affected_agents.items():
        record = state.agents.get(agent_name)
        if record is None:
            continue
        if record.state is AgentState.BLOCKED and record.waiting_for == approved_aid:
            new_record = transition(record, AgentState.RUNNING)
            new_record.waiting_for = None
            state.agents[agent_name] = new_record

    write_state(state)

    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    for aid, action in new_actions.items():
        session = action.agent_name
        record = state.agents.get(action.agent_name)
        if record and record.tmux_session:
            session = record.tmux_session
        _send_writeback(tmux, session, aid, reason)

    ids = ", ".join(action_ids)
    return make_text_result(f"rejected: {ids} — reason: {reason!r}")


__all__ = ["TOOL", "handle"]