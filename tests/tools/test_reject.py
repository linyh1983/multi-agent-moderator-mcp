"""Tests for the real ``reject`` MCP tool (ticket 04).

Coverage:

- Happy path: every listed action transitions to ``rejected``;
  ``decided_at`` and ``reject_reason`` are set; a writeback
  ``<moderator-reject action-id="a-N" reason="...">`` is sent to
  the corresponding agent's tmux session.
- Single-wait invariant (ADR-0009 §4.1): rejecting the action the
  agent is blocked on transitions ``blocked → running`` and
  clears ``waiting_for``. Rejecting a different action leaves the
  block in place.
- All-or-nothing batch (ADR-0009 §4.2): same rules as approve.
- ``reject`` does NOT auto-create any new pending action
  (ADR-0009 §4.3).
- Reject privacy (ADR-0009 §4.6): the full ``reason`` is stored in
  ``state.actions[id].reject_reason``. A chat entry is appended
  with the reason text AND a ``private_to_originator`` metadata
  flag set to the originating agent's name. The check tool (and
  any renderer) treats this as private; the test pins the
  metadata shape directly.
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
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{name}", "agent-stub")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reject_happy_path_transitions_to_rejected() -> None:
    _seed(
        "coder-1",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-1",
                content="rm -rf /",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    res = _run(
        call_tool(
            "reject",
            {"action_ids": ["a-1"], "reason": "too dangerous"},
        )
    )
    assert res.isError is False, res.content[0].text
    assert read_state().actions["a-1"].status == "rejected"


def test_reject_records_reject_reason() -> None:
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
    _run(
        call_tool(
            "reject", {"action_ids": ["a-1"], "reason": "missing tests"}
        )
    )
    a = read_state().actions["a-1"]
    assert a.reject_reason == "missing tests"


def test_reject_records_decided_at() -> None:
    _seed(
        "coder-2b",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-2b",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(
        call_tool("reject", {"action_ids": ["a-1"], "reason": "nope"})
    )
    a = read_state().actions["a-1"]
    assert a.decided_at is not None


def test_reject_writes_moderator_reject_writeback() -> None:
    _seed(
        "coder-3",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-3",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(
        call_tool(
            "reject",
            {"action_ids": ["a-1"], "reason": "missing email field"},
        )
    )
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    assert isinstance(tmux, LocalExecutor)
    tmux.wait_for_buffer_at_least("mod-coder-3", 64)
    pane = tmux.capture_pane("mod-coder-3", lines=50)
    assert "<moderator-reject" in pane
    assert "a-1" in pane
    assert "missing email field" in pane


# ---------------------------------------------------------------------------
# Single-wait invariant
# ---------------------------------------------------------------------------


def test_reject_unblocks_blocked_agent_when_id_matches() -> None:
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
    _run(call_tool("reject", {"action_ids": ["a-1"], "reason": "no"}))
    rec = read_state().agents["coder-4"]
    assert rec.state == AgentState.RUNNING
    assert rec.waiting_for is None


def test_reject_does_not_unblock_when_id_mismatches() -> None:
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
    _run(call_tool("reject", {"action_ids": ["a-2"], "reason": "no"}))
    rec = read_state().agents["coder-5"]
    assert rec.state == AgentState.BLOCKED
    assert rec.waiting_for == "a-1"
    assert read_state().actions["a-2"].status == "rejected"


# ---------------------------------------------------------------------------
# ADR-0009 §4.3: reject does NOT auto-create a new pending action
# ---------------------------------------------------------------------------


def test_reject_does_not_create_new_pending_action() -> None:
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
    _run(call_tool("reject", {"action_ids": ["a-1"], "reason": "no"}))
    state = read_state()
    # Exactly one action; the rejected one. No new pending created.
    assert len(state.actions) == 1
    assert state.actions["a-1"].status == "rejected"
    pending = [a for a in state.actions.values() if a.status == "pending"]
    assert pending == []


# ---------------------------------------------------------------------------
# All-or-nothing batch
# ---------------------------------------------------------------------------


def test_reject_batch_all_or_nothing_missing_id() -> None:
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
            )
        ],
    )
    res = _run(
        call_tool(
            "reject", {"action_ids": ["a-1", "ghost"], "reason": "nope"}
        )
    )
    assert res.isError is True
    assert read_state().actions["a-1"].status == "pending"


def test_reject_batch_all_or_nothing_non_pending() -> None:
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
                status="approved",
                created_at=_utc_now_naive(),
                decided_at=_utc_now_naive(),
            ),
        ],
    )
    res = _run(
        call_tool(
            "reject", {"action_ids": ["a-1", "a-2"], "reason": "nope"}
        )
    )
    assert res.isError is True
    assert read_state().actions["a-1"].status == "pending"
    assert read_state().actions["a-2"].status == "approved"


def test_reject_batch_happy_path() -> None:
    _seed(
        "coder-9",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-9",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            ),
            ApprovalAction(
                id="a-2",
                seq=2,
                agent_name="coder-9",
                content="y",
                status="pending",
                created_at=_utc_now_naive(),
            ),
        ],
    )
    res = _run(
        call_tool(
            "reject",
            {"action_ids": ["a-1", "a-2"], "reason": "nope"},
        )
    )
    assert res.isError is False, res.content[0].text
    actions = read_state().actions
    assert actions["a-1"].status == "rejected"
    assert actions["a-2"].status == "rejected"


# ---------------------------------------------------------------------------
# Reject privacy (ADR-0009 §4.6) — chat entry with private_to_originator
# ---------------------------------------------------------------------------


def test_reject_writes_chat_entry_with_private_to_originator() -> None:
    _seed(
        "coder-10",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-10",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    _run(
        call_tool(
            "reject",
            {"action_ids": ["a-1"], "reason": "missing email field"},
        )
    )
    state = read_state()
    # Exactly one chat entry, the private one.
    assert len(state.chat) == 1
    msg = state.chat[0]
    assert msg.kind == "system"
    assert msg.text == "missing email field"
    assert msg.metadata is not None
    assert msg.metadata.get("private_to_originator") == "coder-10"


def test_reject_chat_entry_per_rejected_action() -> None:
    """Two rejected actions ⇒ two private chat entries."""
    _seed(
        "coder-11",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-11",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            ),
            ApprovalAction(
                id="a-2",
                seq=2,
                agent_name="coder-11",
                content="y",
                status="pending",
                created_at=_utc_now_naive(),
            ),
        ],
    )
    _run(
        call_tool(
            "reject",
            {"action_ids": ["a-1", "a-2"], "reason": "nope"},
        )
    )
    state = read_state()
    private = [
        m
        for m in state.chat
        if m.metadata and m.metadata.get("private_to_originator")
    ]
    assert len(private) == 2


# ---------------------------------------------------------------------------
# Missing args
# ---------------------------------------------------------------------------


def test_reject_no_action_ids_returns_error() -> None:
    res = _run(call_tool("reject", {}))
    assert res.isError is True
    assert "Traceback" not in res.content[0].text


def test_reject_no_reason_returns_error() -> None:
    """Reason is mandatory — there must be a 'why' for every reject."""
    _seed(
        "coder-12",
        actions=[
            ApprovalAction(
                id="a-1",
                seq=1,
                agent_name="coder-12",
                content="x",
                status="pending",
                created_at=_utc_now_naive(),
            )
        ],
    )
    res = _run(call_tool("reject", {"action_ids": ["a-1"]}))
    assert res.isError is True
    # a-1 must remain pending.
    assert read_state().actions["a-1"].status == "pending"
