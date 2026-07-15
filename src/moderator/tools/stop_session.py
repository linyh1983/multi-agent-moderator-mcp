"""``stop_session`` MCP tool (ticket 03 — real shutdown).

Behavior (per ADR-0008 §2.3 and ADR-0009 §4.4):

- ``stop_session(name, grace_seconds=30, force=False)``
- On a normal call (force=False):
    1. Send a wrap-up cue to the agent's tmux session:
       ``<moderator-cue>please wrap up; you have N seconds</moderator-cue>``
    2. Sleep ``grace_seconds``.
    3. ``tmux kill-session`` (errors swallowed — the session may
       already be gone).
- On ``force=True``:
    1. ``tmux kill-session`` immediately.
- Either way, transition the AgentRecord to ``STOPPED`` via the
  lifecycle matrix (illegal transitions bubble up; the tool
  surfaces a clean error).
- Auto-cancel all of the agent's ``pending`` ApprovalActions with
  ``cancelled_reason='agent_stopped'``. Already-decided actions
  (approved/rejected) are not touched (ADR-0009 §4.4 — audit log
  must be intact).
- ``stopped`` records are RETAINED on disk with full postmortem
  fields (ADR-0008 §2.6).
- Idempotent on already-stopped: returns success and does nothing
  destructive.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.lifecycle import (
    IllegalTransition,
    transition as lifecycle_transition,
)
from moderator.core.models import (
    AgentState,
    ApprovalAction,
    _utc_now_naive,
)
from moderator.drivers import DriverError
from moderator.runtime import get_drivers
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error, make_text_result


TOOL = Tool(
    name="stop_session",
    description=(
        "Stop one or all agents. With force=True the tmux session is "
        "killed immediately. Otherwise the agent is sent a wrap-up cue, "
        "given grace_seconds (default 30) to wrap up, then killed. The "
        "AgentRecord is retained on disk with stopped_at for postmortem, "
        "and all of the agent's pending ApprovalActions are auto-cancelled "
        "with cancelled_reason='agent_stopped'."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Agent to stop. Omit to stop all (not yet supported).",
            },
            "force": {
                "type": "boolean",
                "description": "If true, skip the graceful-shutdown wait.",
                "default": False,
            },
            "grace_seconds": {
                "type": "integer",
                "description": "Seconds to wait before SIGKILL. Default 30.",
                "default": 30,
                "minimum": 0,
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)


_DEFAULT_GRACE_SECONDS = 30


# Sleep hook — tests inject a no-op to avoid 30-second waits.
_sleep = time.sleep


# ---------------------------------------------------------------------------
# Auto-cancel helper (ADR-0009 §4.4)
# ---------------------------------------------------------------------------


def _cancel_pending_actions(
    actions: dict[str, ApprovalAction], agent_name: str
) -> int:
    """Cancel all pending actions for ``agent_name``. Returns count."""
    now = _utc_now_naive()
    cancelled = 0
    for action in actions.values():
        if action.agent_name == agent_name and action.status == "pending":
            updated = action.model_copy(
                update={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_reason": "agent_stopped",
                }
            )
            actions[action.id] = updated
            cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# Driver interaction
# ---------------------------------------------------------------------------


def _graceful_kill(
    *, tmux_session: str, grace_seconds: int, tmux, _sleep=_sleep
) -> None:
    """Send the wrap-up cue, wait, then kill. Errors are swallowed."""
    if not tmux.is_alive(tmux_session):
        return
    cue = (
        f"<moderator-cue>please wrap up; you have "
        f"{grace_seconds} seconds</moderator-cue>"
    )
    try:
        tmux.send_keys(tmux_session, cue)
    except DriverError:
        pass
    if grace_seconds > 0:
        _sleep(grace_seconds)
    try:
        tmux.kill_session(tmux_session)
    except DriverError:
        pass


def _force_kill(*, tmux_session: str, tmux) -> None:
    if not tmux.is_alive(tmux_session):
        return
    try:
        tmux.kill_session(tmux_session)
    except DriverError:
        pass


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    name = arguments.get("name")
    if not name:
        return make_error("stop_session: missing required arg: name")

    force: bool = bool(arguments.get("force", False))
    grace_seconds: int = int(
        arguments.get("grace_seconds", _DEFAULT_GRACE_SECONDS)
    )

    state = read_state()
    record = state.agents.get(name)
    if record is None:
        return make_error(
            f"stop_session: no such agent: {name!r}"
        )

    # Idempotent path: already stopped. Refresh nothing, return success.
    if record.state is AgentState.STOPPED:
        return make_text_result(
            f"stop_session: {name!r} already stopped"
        )

    # Acquire drivers only when we need them. (Idempotent path above
    # doesn't touch the driver pair.)
    _, tmux = get_drivers()

    tmux_session = record.tmux_session
    if force:
        if tmux_session:
            _force_kill(tmux_session=tmux_session, tmux=tmux)
    else:
        if tmux_session:
            # Pass _sleep explicitly so tests can monkeypatch the
            # module-level _sleep without the default being frozen
            # at function-definition time.
            _graceful_kill(
                tmux_session=tmux_session,
                grace_seconds=grace_seconds,
                tmux=tmux,
                _sleep=_sleep,
            )

    # Apply the lifecycle transition. IllegalTransition is bubbled up
    # as a clean error — the record is left unchanged on disk.
    try:
        new_record = lifecycle_transition(record, AgentState.STOPPED)
    except IllegalTransition as exc:
        return make_error(
            f"stop_session({name!r}): {exc}"
        )

    state.agents[name] = new_record

    # Auto-cancel pending actions (ADR-0009 §4.4).
    cancelled = _cancel_pending_actions(state.actions, name)

    write_state(state)

    return make_text_result(
        f"stop_session: {name!r} → stopped "
        f"(grace={grace_seconds}s, force={force}, "
        f"cancelled_pending={cancelled})"
    )


__all__ = ["TOOL", "handle"]
