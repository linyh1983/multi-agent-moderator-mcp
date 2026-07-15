"""Tests for the worker poll loop.

Covers:

- ``process_agent`` against a LocalExecutor-backed session:
  captures pane content, dispatches PROGRESS events, advances
  the per-agent offset.
- Bytes flowing through the echo agent produce PROGRESS entries
  in ``state.progress[name]``.
- ``process_agent`` is a no-op when the session is dead.
- ``process_agent`` raises ``KeyError`` for unknown agents.
- Multiple cycles advance the offset monotonically.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.core.models import (
    AgentRecord,
    AgentState,
    empty_state,
)
from moderator.drivers.local import LocalExecutor
from moderator.state.store import write_state
from moderator.worker import WorkerConfig, WorkerStats, process_agent


@pytest.fixture
def executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[LocalExecutor, None, None]:
    """Per-test driver + isolated state file. Worker tests would
    otherwise pollute the user's real ``~/.moderator/state.json``."""
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    workdir = tmp_path / "remote"
    workdir.mkdir(parents=True, exist_ok=True)
    exe = LocalExecutor(workdir=workdir)
    try:
        yield exe
    finally:
        try:
            exe.close()
        except Exception:
            pass


def _seed_agent(
    executor: LocalExecutor, name: str = "coder-1", project_dir: str = "~/p"
) -> None:
    """Write a starting-state record and create the session."""
    executor.create_session(f"mod-{name}", "ignored")
    state = empty_state()
    state.agents[name] = AgentRecord(
        name=name,
        host="user@local",
        project_dir=project_dir,
        tmux_session=f"mod-{name}",
        state=AgentState.RUNNING,
    )
    write_state(state)


def test_process_agent_raises_for_unknown_agent(executor: LocalExecutor) -> None:
    stats = WorkerStats()
    with pytest.raises(KeyError):
        process_agent(
            name="nope",
            tmux=executor,
            config=WorkerConfig(),
            stats=stats,
        )


def test_process_agent_is_noop_when_session_dead(executor: LocalExecutor) -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
    )
    write_state(state)
    # No create_session call → session is not alive.
    stats = WorkerStats()
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=stats,
    )
    assert record.state is AgentState.RUNNING
    assert stats.cycles == 0  # no cycle counted for a dead session


def test_process_agent_writes_progress_entry_for_marker(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    _seed_agent(executor)
    executor.send_keys(
        "mod-coder-1", "【进度汇报】step 1 done【/进度汇报】"
    )
    # Wait for the echo agent to round-trip the bytes.
    assert executor.wait_for_buffer_at_least("mod-coder-1", 64)

    stats = WorkerStats()
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=stats,
    )

    from moderator.state.store import read_state

    state = read_state()
    assert "coder-1" in state.progress
    assert len(state.progress["coder-1"]) == 1
    assert state.progress["coder-1"][0].text == "step 1 done"
    assert stats.progress_written == 1
    assert stats.cycles == 1
    assert stats.bytes_read > 0


def test_process_agent_does_not_double_count_across_cycles(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    """The second cycle sees no NEW bytes → no new progress entry."""
    _seed_agent(executor)
    executor.send_keys("mod-coder-1", "【进度汇报】only one【/进度汇报】")
    assert executor.wait_for_buffer_at_least("mod-coder-1", 64)

    stats = WorkerStats()
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=stats
    )
    # Second cycle, no new bytes sent.
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=stats
    )

    from moderator.state.store import read_state

    state = read_state()
    assert len(state.progress["coder-1"]) == 1


def test_process_agent_advances_offset_monotonically(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    _seed_agent(executor)
    executor.send_keys("mod-coder-1", "【进度汇报】a【/进度汇报】")
    assert executor.wait_for_buffer_at_least("mod-coder-1", 32)
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=WorkerStats()
    )
    from moderator.state.store import read_state

    offset_after_first = read_state().agents["coder-1"].log_offset
    assert offset_after_first > 0

    executor.send_keys("mod-coder-1", "【进度汇报】b【/进度汇报】")
    assert executor.wait_for_buffer_at_least("mod-coder-1", offset_after_first + 32)
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=WorkerStats()
    )
    offset_after_second = read_state().agents["coder-1"].log_offset
    assert offset_after_second > offset_after_first


def test_process_agent_updates_last_output_at_on_bytes(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    _seed_agent(executor)
    executor.send_keys("mod-coder-1", "plain log line, no marker")
    assert executor.wait_for_buffer_at_least("mod-coder-1", 32)
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=WorkerStats()
    )
    from moderator.state.store import read_state

    assert read_state().agents["coder-1"].last_output_at is not None


def test_process_agent_records_events_for_dispatch(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    _seed_agent(executor)
    executor.send_keys(
        "mod-coder-1", "【进度汇报】one【/进度汇报】text【进度汇报】two【/进度汇报】"
    )
    # The pane accumulates "<<cmd: ignored>>" + our two markers.
    assert executor.wait_for_buffer_at_least("mod-coder-1", 64)

    stats = WorkerStats()
    process_agent(
        name="coder-1", tmux=executor, config=WorkerConfig(), stats=stats
    )
    from moderator.markers import MarkerKind

    kinds = [e.kind for e in stats.events]
    assert kinds.count(MarkerKind.PROGRESS) == 2
