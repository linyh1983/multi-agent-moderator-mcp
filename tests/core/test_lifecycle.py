"""Tests for the AgentState transition matrix (ADR-0008 §2.1).

The matrix is the single source of truth for legal state changes.
Workers, tools, and the CLI all funnel through :func:`transition` so
that an illegal move raises immediately rather than corrupting
``state.agents``.

Tickets covered: 03 (full 7-state lifecycle observable; illegal
transitions raise).
"""

from __future__ import annotations

import pytest

from moderator.core.lifecycle import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    can_transition,
    transition,
)
from moderator.core.models import AgentRecord, AgentState


def _record(*, state: AgentState = AgentState.STARTING) -> AgentRecord:
    return AgentRecord(
        name="a-1",
        host="user@fake",
        project_dir="~/proj",
        state=state,
    )


# ---------------------------------------------------------------------------
# Every legal transition from ADR-0008 §2.1 — the matrix is exhaustive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src, dst",
    [
        (AgentState.STARTING, AgentState.RUNNING),
        (AgentState.RUNNING, AgentState.BLOCKED),
        (AgentState.RUNNING, AgentState.STUCK),
        (AgentState.RUNNING, AgentState.OFFLINE),
        (AgentState.RUNNING, AgentState.STOPPED),
        (AgentState.BLOCKED, AgentState.RUNNING),
        (AgentState.STUCK, AgentState.RUNNING),
        (AgentState.OFFLINE, AgentState.RUNNING),
        (AgentState.STUCK, AgentState.STOPPED),
        (AgentState.OFFLINE, AgentState.STOPPED),
        (AgentState.ERROR, AgentState.STOPPED),
    ],
)
def test_legal_transition_returns_record_in_new_state(
    src: AgentState, dst: AgentState
) -> None:
    rec = _record(state=src)
    out = transition(rec, dst)
    assert out.state == dst


# ---------------------------------------------------------------------------
# Illegal transitions raise. A few representative pairs; the
# exhaustive check lives in test_legal_transitions_match_adr_table.
# ---------------------------------------------------------------------------


def test_stopped_is_terminal() -> None:
    """`stopped` is terminal per ADR-0008 §2.6. No state can follow it."""
    rec = _record(state=AgentState.STOPPED)
    for target in AgentState:
        with pytest.raises(IllegalTransition):
            transition(rec, target)


def test_error_cannot_recover_to_running() -> None:
    """ADR-0008 §2.5: error never auto-recovers. Must stop_session
    first, then start_session with a fresh record."""
    rec = _record(state=AgentState.ERROR)
    with pytest.raises(IllegalTransition):
        transition(rec, AgentState.RUNNING)


def test_starting_cannot_skip_to_stuck() -> None:
    """A `starting` record must reach `running` before any other
    lifecycle state. The first observable move is success/failure of
    ``start_session`` itself."""
    rec = _record(state=AgentState.STARTING)
    with pytest.raises(IllegalTransition):
        transition(rec, AgentState.STUCK)


def test_starting_cannot_go_straight_to_stopped() -> None:
    rec = _record(state=AgentState.STARTING)
    with pytest.raises(IllegalTransition):
        transition(rec, AgentState.STOPPED)


def test_blocked_cannot_jump_to_stuck_or_offline() -> None:
    """A blocked agent is paused waiting on a moderator decision; the
    legal exits are back to ``running`` (approve/reject),
    ``stopped`` (moderator gives up waiting and kills it), or
    ``error`` (a driver-level failure surfaces). Going to
    ``stuck`` / ``offline`` is not a coherent state change — blocked
    is a deliberate wait, not a failure mode."""
    rec = _record(state=AgentState.BLOCKED)
    for dst in (AgentState.STUCK, AgentState.OFFLINE):
        with pytest.raises(IllegalTransition):
            transition(rec, dst)


# ---------------------------------------------------------------------------
# can_transition() is a non-raising predicate
# ---------------------------------------------------------------------------


def test_can_transition_true_for_legal() -> None:
    assert can_transition(AgentState.RUNNING, AgentState.BLOCKED) is True


def test_can_transition_false_for_illegal() -> None:
    assert can_transition(AgentState.STOPPED, AgentState.RUNNING) is False


# ---------------------------------------------------------------------------
# The matrix covers all 7 states on the LHS and matches the ADR table
# ---------------------------------------------------------------------------


def test_legal_transitions_match_adr_table() -> None:
    """Every AgentState must appear as a source (a dead-end state
    like `stopped` has an empty set)."""
    assert set(LEGAL_TRANSITIONS.keys()) == set(AgentState)


def test_legal_transitions_table_matches_adr_0008_2_1() -> None:
    """Pinned: if this drifts from ADR-0008 §2.1 the ADR must change
    first and the test must move with it."""
    expected: dict[AgentState, set[AgentState]] = {
        AgentState.STARTING: {AgentState.RUNNING, AgentState.ERROR},
        AgentState.RUNNING: {
            AgentState.BLOCKED,
            AgentState.STUCK,
            AgentState.OFFLINE,
            AgentState.STOPPED,
            AgentState.ERROR,
        },
        AgentState.BLOCKED: {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR},
        AgentState.STUCK: {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR},
        AgentState.OFFLINE: {AgentState.RUNNING, AgentState.STOPPED, AgentState.ERROR},
        AgentState.STOPPED: set(),  # terminal
        AgentState.ERROR: {AgentState.STOPPED},
    }
    assert {k: set(v) for k, v in LEGAL_TRANSITIONS.items()} == expected


# ---------------------------------------------------------------------------
# transition() mutates the timestamp fields implied by the move
# ---------------------------------------------------------------------------


def test_transition_to_stopped_records_stopped_at() -> None:
    rec = _record(state=AgentState.RUNNING)
    out = transition(rec, AgentState.STOPPED)
    assert out.stopped_at is not None


def test_transition_to_non_stopped_leaves_stopped_at_untouched() -> None:
    rec = _record(state=AgentState.RUNNING)
    out = transition(rec, AgentState.BLOCKED)
    assert out.stopped_at is None


# ---------------------------------------------------------------------------
# transition() is pure: does not mutate the input record
# ---------------------------------------------------------------------------


def test_transition_does_not_mutate_input_record() -> None:
    rec = _record(state=AgentState.RUNNING)
    _ = transition(rec, AgentState.BLOCKED)
    assert rec.state == AgentState.RUNNING
