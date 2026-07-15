"""Tests for the real ``approve`` MCP tool (ticket 04).

Coverage:

- Happy path: every listed action transitions to ``approved``;
  ``decided_at`` is set; a writeback
  ``<moderator-approve action-id="a-N" note="...">`` is sent to
  the corresponding agent's tmux session.
- Single-wait invariant (ADR-0009 §4.1): if the agent is
  ``blocked`` on the approved action_id, transition
  ``blocked → running`` and clear ``waiting_for``. If the agent
  is blocked on a DIFFERENT action, do NOT unblock.
- All-or-nothing batch (ADR-0009 §4.2): if ANY id is missing or
  non-pending, the entire call errors and no state changes.
- Missing / empty args: clean error, no stacktrace.
- Note is optional; the writeback omits the ``note`` attribute
  when not provided.
- The agent record is updated to reflect the cleared
  ``waiting_for`` after approval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.core.models import (
    AgentRecord,
    AgentState,
    ApprovalAction,
    _utc_now_naive,
    empty_state,
)
from moderator.drivers.local import LocalExecutor
from moderator.runtime import reset_drivers, set_drivers
from moderator.state.store import read_state, write_state
from moderator.tools import call_tool


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    workdir = tmp_path / "local-exec"
    workdir.mkdir(parents=True, exist_ok=True)
    execu = LocalExecutor(workdir=workdir)
    set_drivers(execu, execu)
    yield
    try:
        execu.close()
    except Exception:
        pass
    reset_drivers()


def _seed(
    name: str,
    *,
    state: AgentState = AgentState.RUNNING,
    waiting_for: str | None = None,
    actions: list[ApprovalAction] | None = None,
) -> None:
    s = empty_state()
    s.agents[name] = AgentRecord(
        name=name,
        host="user@local-exec",
        project_dir="~/p",
        state=state,
        tmux_session=f"mod-{name}",
        waiting_for=waiting_for,
    )
    s.action_seq = len(actions or [])
    for i, a in enumerate(actions or [], start=1):
        a = a.model_copy(update={"id": f"a-{i}", "seq": i})
        s.actions[f"a-{i}"] = a
    write_state(s)
    # Spin up a session so writeback doesn't crash.
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{name}", "agent-stub")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_approve_happy_path_transitions_to_approved() -> None:
    _seed(
        "coder-1",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-1",
                content="git push",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    res = _run(call_tool("approve", {"action_ids": ["a-1"]}))
    assert res.isError is False, res.content[0].text
    assert read_state().actions["a-1"].status == "approved"


def test_approve_records_decided_at() -> None:
    _seed(
        "coder-2",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-2",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-1"]}))
    a = read_state().actions["a-1"]
    assert a.decided_at is not None


def test_approve_writes_moderator_approve_writeback() -> None:
    """The writeback is a single line containing action-id and note."""
    _seed(
        "coder-3",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-3",
                content="git push",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-1"], "note": "LGTM"}))
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    # Wait for the echo agent to actually echo the writeback into
    # stdout_buf. Without this, capture_pane can race the daemon
    # reader thread.
    assert isinstance(tmux, LocalExecutor)
    tmux.wait_for_buffer_at_least("mod-coder-3", 64)
    pane = tmux.capture_pane("mod-coder-3", lines=50)
    assert "<moderator-approve" in pane
    assert "a-1" in pane
    assert "LGTM" in pane


def test_approve_without_note_omits_note_attribute() -> None:
    _seed(
        "coder-3b",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-3b",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-1"]}))
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    assert isinstance(tmux, LocalExecutor)
    tmux.wait_for_buffer_at_least("mod-coder-3b", 64)
    pane = tmux.capture_pane("mod-coder-3b", lines=50)
    assert "<moderator-approve" in pane
    # The plain writeback line should not have a `note=` attribute.
    # (Filter to the moderator-approve line; other lines from the
    # echo agent may legitimately mention "note" in unrelated text.)
    approve_lines = [
        ln for ln in pane.splitlines() if "<moderator-approve" in ln
    ]
    assert approve_lines
    assert all("note=" not in ln for ln in approve_lines)


# ---------------------------------------------------------------------------
# Single-wait invariant (ADR-0009 §4.1)
# ---------------------------------------------------------------------------


def test_approve_unblocks_blocked_agent_when_id_matches() -> None:
    _seed(
        "coder-4",
        state=AgentState.BLOCKED,
        waiting_for="a-1",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-4",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-1"]}))
    rec = read_state().agents["coder-4"]
    assert rec.state == AgentState.RUNNING
    assert rec.waiting_for is None


def test_approve_does_not_unblock_when_id_mismatches() -> None:
    """Single-wait invariant: only the exact action_id unblocks the
    agent. Approving a different action must leave the block in place."""
    _seed(
        "coder-5",
        state=AgentState.BLOCKED,
        waiting_for="a-1",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-5",
                content="waiting",
                status="pending",
                created_at=_utc_now_naive(),
            ),
            ApprovalAction(
                id="a-2",
                seq=2,
                agent_name="coder-5",
                content="other",
                status="pending",
                created_at=_utc_now_naive(),
            ),
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-2"]}))
    rec = read_state().agents["coder-5"]
    assert rec.state == AgentState.BLOCKED
    assert rec.waiting_for == "a-1"
    # But a-2 is now approved.
    assert read_state().actions["a-2"].status == "approved"


def test_approve_for_other_agent_does_not_unblock_target() -> None:
    """Approving an action whose agent_name is X must not touch an
    unrelated blocked agent Y."""
    _seed(
        "blocked-agent",
        state=AgentState.BLOCKED,
        waiting_for="a-2",
        actions=[
            ApprovalAction(
                id="a-2",
                seq=1,
                agent_name="blocked-agent",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            ),
        ],
    )
    # Add an unrelated pending action for a different agent by
    # writing state directly.
    state = read_state()
    state.agents["other-agent"] = AgentRecord(
        name="other-agent",
        host="user@local-exec",
        project_dir="~/p",
        state=AgentState.RUNNING,
        tmux_session="mod-other-agent",
    )
    state.actions["a-3"] = ApprovalAction(
        id="a-3",
        seq=2,
        agent_name="other-agent",
        content="y",
        status="pending",
        created_at=_utc_now_naive(),
    )
    state.action_seq = 2
    write_state(state)
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session("mod-other-agent", "stub")

    _run(call_tool("approve", {"action_ids": ["a-3"]}))
    rec = read_state().agents["blocked-agent"]
    assert rec.state == AgentState.BLOCKED
    assert rec.waiting_for == "a-2"


# ---------------------------------------------------------------------------
# All-or-nothing batch (ADR-0009 §4.2)
# ---------------------------------------------------------------------------


def test_approve_batch_all_or_nothing_missing_id() -> None:
    _seed(
        "coder-6",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-6",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    res = _run(call_tool("approve", {"action_ids": ["a-1", "ghost"]}))
    assert res.isError is True
    # a-1 must remain pending.
    assert read_state().actions["a-1"].status == "pending"


def test_approve_batch_all_or_nothing_non_pending() -> None:
    _seed(
        "coder-7",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-7",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            ),
            ApprovalAction(
                id="a-2",
                seq=2,
                agent_name="coder-7",
                content="y",
                status="approved",  # already decided
                created_at=_utc_now_naive(),
                decided_at=_utc_now_naive(),
            ),
        ],
    )
    res = _run(call_tool("approve", {"action_ids": ["a-1", "a-2"]}))
    assert res.isError is True
    # a-1 must remain pending; a-2 must remain approved (not flipped).
    assert read_state().actions["a-1"].status == "pending"
    assert read_state().actions["a-2"].status == "approved"


def test_approve_batch_happy_path() -> None:
    _seed(
        "coder-8",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-8",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            ),
            ApprovalAction(
                id="a-2",
                seq=2,
                agent_name="coder-8",
                content="y",
                status="pending",
                created_at=_utc_now_naive(),
            ),
        ],
    )
    res = _run(
        call_tool("approve", {"action_ids": ["a-1", "a-2"], "note": "ok"})
    )
    assert res.isError is False, res.content[0].text
    actions = read_state().actions
    assert actions["a-1"].status == "approved"
    assert actions["a-2"].status == "approved"


# ---------------------------------------------------------------------------
# Missing / empty args
# ---------------------------------------------------------------------------


def test_approve_no_action_ids_returns_error() -> None:
    res = _run(call_tool("approve", {}))
    assert res.isError is True
    assert "Traceback" not in res.content[0].text


def test_approve_empty_action_ids_returns_error() -> None:
    res = _run(call_tool("approve", {"action_ids": []}))
    assert res.isError is True
    assert "Traceback" not in res.content[0].text


# ---------------------------------------------------------------------------
# Approve when agent is running (not blocked) — no state change, action
# still goes to approved and writeback still sent.
# ---------------------------------------------------------------------------


def test_approve_running_agent_does_not_change_state() -> None:
    _seed(
        "coder-9",
        state=AgentState.RUNNING,
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-9",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(call_tool("approve", {"action_ids": ["a-1"]}))
    rec = read_state().agents["coder-9"]
    assert rec.state == AgentState.RUNNING
    assert rec.waiting_for is None
    assert read_state().actions["a-1"].status == "approved"
