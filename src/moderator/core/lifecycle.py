"""Agent lifecycle transition matrix (ADR-0008 §2.1).

The seven ``AgentState`` values and the legal moves between them are
captured in :data:`LEGAL_TRANSITIONS`. Every state-changing
operation in the moderator (worker poll, ``start_session``,
``stop_session``, approval/reject writeback) goes through
:func:`transition` so that an illegal move raises immediately
rather than corrupting ``state.agents``.

The matrix is pinned by ``tests/core/test_lifecycle.py`` against
the ADR table — change the ADR first, then update the test, then
update this module.
"""

from __future__ import annotations

from moderator.core.models import (
    AgentRecord,
    AgentState,
    _utc_now_naive,
)


# ---------------------------------------------------------------------------
# Transition matrix — single source of truth.
# ---------------------------------------------------------------------------

LEGAL_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.STARTING: frozenset({AgentState.RUNNING, AgentState.ERROR}),
    AgentState.RUNNING: frozenset(
        {
            AgentState.BLOCKED,
            AgentState.STUCK,
            AgentState.OFFLINE,
            AgentState.STOPPED,
            AgentState.ERROR,
        }
    ),
    AgentState.BLOCKED: frozenset(
        {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR}
    ),
    AgentState.STUCK: frozenset(
        {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR}
    ),
    AgentState.OFFLINE: frozenset(
        {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR}
    ),
    AgentState.STOPPED: frozenset(),  # terminal
    AgentState.ERROR: frozenset({AgentState.STOPPED}),
}


class IllegalTransition(ValueError):
    """Raised when a caller asks for a state move the matrix forbids.

    The error message names both states so logs and tests can
    pinpoint the offender without re-reading the matrix.
    """

    def __init__(self, src: AgentState, dst: AgentState) -> None:
        super().__init__(
            f"illegal AgentState transition: {src.value!r} → {dst.value!r}"
        )
        self.src = src
        self.dst = dst


def can_transition(src: AgentState, dst: AgentState) -> bool:
    """Return True iff ``src → dst`` is in the matrix.

    Pure predicate — does not raise. Useful for conditional logic
    (e.g. "if I can transition, do it; otherwise log") that doesn't
    want to wrap :func:`transition` in a try/except.
    """
    return dst in LEGAL_TRANSITIONS.get(src, frozenset())


def transition(record: AgentRecord, target: AgentState) -> AgentRecord:
    """Return a copy of ``record`` with state set to ``target``.

    The function is pure: it does NOT mutate the input. Side
    effects (writing the record to disk, killing the tmux session)
    are the caller's responsibility.

    Timestamp fields are updated to reflect the move:

    - ``stopped_at`` is set when ``target is AgentState.STOPPED``
      (ADR-0008 §2.6: postmortem must be possible).
    - Other fields are passed through unchanged.

    Raises:
        IllegalTransition: if the move is not in the matrix.
    """
    if not can_transition(record.state, target):
        raise IllegalTransition(record.state, target)

    updates: dict[str, object] = {"state": target}
    if target is AgentState.STOPPED:
        updates["stopped_at"] = _utc_now_naive()
    return record.model_copy(update=updates)


__all__ = [
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "can_transition",
    "transition",
]
