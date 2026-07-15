"""Tests for the worker's REQUEST_EXEC marker handling (ticket 04).

When the worker sees ``【申请执行】<content>【/申请执行】`` in an
agent's stdout it must:

- Create a new :class:`ApprovalAction` in ``state.actions`` with
  ``id='a-<seq>'``, ``status='pending'``, ``created_at``, and
  ``agent_name``. The sequence comes from ``state.action_seq``,
  which is bumped after each new action.
- Send a writeback ack ``<moderator-info action-id='a-N'
  status='queued'>`` to the agent's tmux session (ADR-0007 §6.1:
  every REQUEST_EXEC gets an ack).
- Transition the agent ``running → blocked`` if the agent was
  ``running``. The single-wait invariant (ADR-0009 §4.1) is
  preserved by recording ``AgentRecord.waiting_for`` = the new
  action_id.
- If the agent is already ``blocked`` on another action, do NOT
  re-transition the state. Still create the new pending action
  (the queue accumulates) but leave ``waiting_for`` alone.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.core.models import (
    AgentRecord,
    AgentState,
    _utc_now_naive,
    empty_state,
)
from moderator.drivers.local import LocalExecutor
from moderator.runtime import reset_drivers, set_drivers
from moderator.state.store import read_state, write_state
from moderator.worker import WorkerConfig, WorkerStats, process_agent


def _run_async(coro):
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


def _seed_running_agent(name: str) -> None:
    state = empty_state()
    state.agents[name] = AgentRecord(
        name=name,
        host="user@local-exec",
        project_dir="~/p",
        state=AgentState.RUNNING,
        tmux_session=f"mod-{name}",
        started_at=_utc_now_naive(),
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)
    # The agent process must actually exist for tmux.send_keys to
    # not raise. LocalExecutor's echo agent will receive and echo
    # whatever we send.
    from moderator.drivers.local import LocalExecutor
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{name}", "agent-stub")


def _send_marker(name: str, marker: str) -> None:
    """Write ``marker`` to the agent's tmux stdin (echoed back)."""
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.send_keys(f"mod-{name}", marker)
    # The echo agent processes one line per call; wait until the
    # daemon reader has caught up so process_agent sees the bytes.
    # Use byte length (UTF-8) since stdout_buf is a bytearray; the
    # ``+ 64`` covers the ``<<cmd: ...>>\n`` prefix that
    # create_session writes (≈20 bytes) plus the trailing newline
    # of the echoed marker.
    assert isinstance(tmux, LocalExecutor)
    tmux.wait_for_buffer_at_least(
        f"mod-{name}", 64 + len(marker.encode("utf-8"))
    )


# ---------------------------------------------------------------------------
# Happy path: REQUEST_EXEC creates an action and transitions state
# ---------------------------------------------------------------------------


def test_request_exec_creates_approval_action() -> None:
    _seed_running_agent("coder-1")
    _send_marker("coder-1", "【申请执行】git push【/申请执行】")

    tmux = LocalExecutor.__new__(LocalExecutor)
    from moderator.runtime import get_drivers

    _, real_tmux = get_drivers()
    process_agent(
        name="coder-1",
        tmux=real_tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    actions = read_state().actions
    assert len(actions) == 1
    aid = next(iter(actions))
    assert actions[aid].agent_name == "coder-1"
    assert actions[aid].content == "git push"
    assert actions[aid].status == "pending"


def test_request_exec_action_id_is_a_seq() -> None:
    _seed_running_agent("coder-2")
    _send_marker("coder-2", "【申请执行】run tests【/申请执行】")

    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    process_agent(
        name="coder-2",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    state = read_state()
    aid = next(iter(state.actions))
    assert aid.startswith("a-")
    assert int(aid[2:]) == 1
    assert state.action_seq == 1


def test_request_exec_increments_action_seq_across_markers() -> None:
    """Two REQUEST_EXEC markers yield a-1 and a-2 in order."""
    _seed_running_agent("coder-3")
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()

    _send_marker("coder-3", "【申请执行】first【/申请执行】")
    process_agent(
        name="coder-3",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    # After the first request, the agent is blocked. Send another
    # REQUEST_EXEC while blocked to verify the seq still increments.
    _send_marker("coder-3", "【申请执行】second【/申请执行】")
    process_agent(
        name="coder-3",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    state = read_state()
    ids = sorted(state.actions.keys())
    assert ids == ["a-1", "a-2"]
    assert state.action_seq == 2


def test_request_exec_transitions_running_to_blocked() -> None:
    _seed_running_agent("coder-4")
    _send_marker("coder-4", "【申请执行】do it【/申请执行】")
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    process_agent(
        name="coder-4",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    rec = read_state().agents["coder-4"]
    assert rec.state == AgentState.BLOCKED


def test_request_exec_records_waiting_for() -> None:
    _seed_running_agent("coder-5")
    _send_marker("coder-5", "【申请执行】do it【/申请执行】")
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    process_agent(
        name="coder-5",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    state = read_state()
    aid = next(iter(state.actions))
    assert state.agents["coder-5"].waiting_for == aid


def test_request_exec_writes_ack_writeback_to_stdin() -> None:
    """ADR-0007 §6.1: every REQUEST_EXEC gets a queued ack."""
    _seed_running_agent("coder-6")
    _send_marker("coder-6", "【申请执行】do it【/申请执行】")
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    process_agent(
        name="coder-6",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    # The LocalExecutor's echo agent will have echoed the ack back
    # into stdout_buf. The ack should appear in capture_pane.
    pane = tmux.capture_pane("mod-coder-6", lines=50)
    assert "<moderator-info" in pane
    assert "status=\"queued\"" in pane
    assert "a-1" in pane


# ---------------------------------------------------------------------------
# Blocked agent receiving a second REQUEST_EXEC: state stays blocked,
# waiting_for stays on the first action, new action is appended.
# ---------------------------------------------------------------------------


def test_request_exec_on_blocked_agent_does_not_change_state() -> None:
    _seed_running_agent("coder-7")
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()

    _send_marker("coder-7", "【申请执行】first【/申请执行】")
    process_agent(
        name="coder-7",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    first_waiting = read_state().agents["coder-7"].waiting_for

    _send_marker("coder-7", "【申请执行】second【/申请执行】")
    process_agent(
        name="coder-7",
        tmux=tmux,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    rec = read_state().agents["coder-7"]
    assert rec.state == AgentState.BLOCKED
    # The agent is still waiting on the FIRST action. The new
    # REQUEST_EXEC is queued in state.actions but doesn't reset the
    # single-wait invariant.
    assert rec.waiting_for == first_waiting
    assert len(read_state().actions) == 2
