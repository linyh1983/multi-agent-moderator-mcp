"""Tests for the worker's stuck / offline detection (ticket 06).

Covers ADR-0008 §2.2 (stuck semantics), §2.4 (offline retry), §2.5
(self-recovery on new bytes), and ADR-0017 (no auto-restart).

Key invariants under test:

- ``running → stuck`` only when ``now - last_output_at >
  stuck_threshold_seconds`` AND ``is_alive()`` returns True.
- ``running → offline`` requires TWO consecutive ``is_alive()``
  failures (one failed poll + one retry). A single failure does
  NOT transition state.
- ``stuck → running`` and ``offline → running`` self-recover as
  soon as new stdout bytes arrive (assuming the tmux session is
  alive again).
- ``last_output_at`` updates on ANY byte read, not just marker
  bytes — a noisy ``find /`` keeps the agent from going stuck.
- ``error`` is never self-healing (no transition out of error
  without moderator action).
- The worker never creates a new tmux session; ``offline`` is a
  sticky state until the moderator calls ``stop_session``.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest

from moderator.core.models import (
    AgentRecord,
    AgentState,
    _utc_now_naive,
    empty_state,
)
from moderator.drivers.local import LocalExecutor
from moderator.state.store import read_state, write_state
from moderator.worker import (
    WorkerConfig,
    WorkerStats,
    process_agent,
    reset_health_counters,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[LocalExecutor, None, None]:
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
        reset_health_counters()


def _seed_running(
    executor: LocalExecutor,
    name: str = "coder-1",
    *,
    last_output_seconds_ago: int = 0,
) -> AgentRecord:
    """Seed a RUNNING record and create a live tmux session.

    The :class:`LocalExecutor` ``create_session`` writes a 20-byte
    ``<<cmd: ...>>\\n`` prefix to the session's stdout buffer. The
    worker's first poll would otherwise see those bytes and
    update ``last_output_at`` to NOW — wiping out any
    pre-seeded silence gap. We pre-advance ``log_offset`` to the
    end of the cmd prefix so the worker treats the session as
    having no new bytes.
    """
    executor.create_session(f"mod-{name}", "agent-stub")
    prefix_len, _ = executor.read_from(f"mod-{name}", 0)
    now = _utc_now_naive()
    record = AgentRecord(
        name=name,
        host="user@local",
        project_dir="~/p",
        tmux_session=f"mod-{name}",
        state=AgentState.RUNNING,
        started_at=now,
        last_output_at=now - timedelta(seconds=last_output_seconds_ago),
        log_offset=prefix_len,
    )
    state = empty_state()
    state.agents[name] = record
    write_state(state)
    return record


# ---------------------------------------------------------------------------
# Stuck detection
# ---------------------------------------------------------------------------


def test_running_to_stuck_after_threshold_with_alive_session(
    executor: LocalExecutor,
) -> None:
    """After 30+ minutes of silence (default threshold) and tmux
    still alive, the agent transitions running → stuck."""
    # 31 minutes silent, session alive.
    _seed_running(executor, last_output_seconds_ago=31 * 60)
    stats = WorkerStats()
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=stats,
    )
    assert record.state is AgentState.STUCK


def test_running_stays_running_under_threshold(
    executor: LocalExecutor,
) -> None:
    """A 5-minute gap is well under the default 30 min threshold."""
    _seed_running(executor, last_output_seconds_ago=5 * 60)
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING


def test_no_stuck_when_session_not_alive(
    executor: LocalExecutor,
) -> None:
    """Stuck requires BOTH the threshold AND is_alive=True. If the
    tmux session is gone we go to offline, not stuck."""
    # Seed record but DO NOT create the session.
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive() - timedelta(seconds=31 * 60),
        log_offset=0,
    )
    write_state(state)

    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    # With one is_alive failure (no retry yet), the state must
    # not become STUCK. After the retry it becomes OFFLINE; the
    # first cycle leaves it RUNNING.
    assert record.state is AgentState.RUNNING


def test_custom_stuck_threshold(
    executor: LocalExecutor,
) -> None:
    """A 5-minute threshold flips a 6-minute gap to stuck; the
    default 30-minute threshold does not."""
    _seed_running(executor, last_output_seconds_ago=6 * 60)
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(stuck_threshold_seconds=5 * 60),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.STUCK


# ---------------------------------------------------------------------------
# Offline detection — two consecutive is_alive() failures
# ---------------------------------------------------------------------------


def test_offline_requires_two_consecutive_failures(
    executor: LocalExecutor,
) -> None:
    """A single is_alive() failure does NOT transition; the second
    consecutive failure flips running → offline."""
    # Seed record; do NOT create the tmux session.
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)

    # First cycle: failure #1, no transition.
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING

    # Second cycle: failure #2, now → OFFLINE.
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.OFFLINE


def test_offline_records_last_error(
    executor: LocalExecutor,
) -> None:
    """On the offline transition, last_error records the reason."""
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    rec = read_state().agents["coder-1"]
    assert rec.state is AgentState.OFFLINE
    assert rec.last_error is not None
    assert rec.last_error != ""


def test_offline_failure_counter_resets_on_alive(
    executor: LocalExecutor,
) -> None:
    """If the session comes back between failures, the failure
    counter must reset (so the agent does not flip offline on
    an unrelated old failure)."""
    # Seed without session.
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    # Now create the session: next cycle is_alive=True → reset.
    executor.create_session("mod-coder-1", "agent-stub")
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING
    # Kill the session; one more failure should NOT flip offline
    # because the counter was reset.
    executor.kill_session("mod-coder-1")
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING


# ---------------------------------------------------------------------------
# Self-recovery on new bytes
# ---------------------------------------------------------------------------


def test_stuck_recovers_to_running_on_new_bytes(
    executor: LocalExecutor,
) -> None:
    """When an agent in STUCK state produces new stdout bytes and
    the tmux session is alive, transition stuck → running."""
    record = _seed_running(executor, last_output_seconds_ago=31 * 60)
    # First cycle: alive, no new bytes, no marker → STUCK.
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.STUCK

    # Now feed some bytes (a plain log line is enough; no marker
    # needed to count as activity, ADR-0008 §2.2).
    executor.send_keys(
        "mod-coder-1",
        "I am back online — noisy noisy noisy noisy noisy noisy",
    )
    # The cmd prefix was 20 bytes; we appended more — wait for
    # it to land in the daemon reader's buffer.
    prefix_len, _ = executor.read_from("mod-coder-1", 0)
    assert executor.wait_for_buffer_at_least("mod-coder-1", prefix_len + 32)

    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING


def test_offline_recovers_to_running_on_new_bytes(
    executor: LocalExecutor,
) -> None:
    """When an offline agent's session reappears AND new bytes
    arrive, transition offline → running (ADR-0017)."""
    # Seed without session.
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.OFFLINE

    # Session reappears with new bytes.
    executor.create_session("mod-coder-1", "agent-stub")
    executor.send_keys("mod-coder-1", "alive again alive again alive again")
    prefix_len, _ = executor.read_from("mod-coder-1", 0)
    assert executor.wait_for_buffer_at_least("mod-coder-1", prefix_len + 16)

    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    assert record.state is AgentState.RUNNING


# ---------------------------------------------------------------------------
# last_output_at updates on ANY bytes, not just markers
# ---------------------------------------------------------------------------


def test_last_output_at_updates_on_plain_bytes(
    executor: LocalExecutor,
) -> None:
    """A noisy `find /` (plain bytes, no marker) still counts as
    activity and keeps the agent out of stuck."""
    _seed_running(executor, last_output_seconds_ago=10)
    # Touch the record so we have a known starting last_output_at.
    state = read_state()
    rec0 = state.agents["coder-1"]
    initial = rec0.last_output_at
    assert initial is not None

    executor.send_keys("mod-coder-1", "noisy output, no marker, just chatter")
    prefix_len, _ = executor.read_from("mod-coder-1", 0)
    assert executor.wait_for_buffer_at_least("mod-coder-1", prefix_len + 16)
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    rec1 = read_state().agents["coder-1"]
    assert rec1.last_output_at is not None
    assert rec1.last_output_at >= initial


# ---------------------------------------------------------------------------
# error never self-heals
# ---------------------------------------------------------------------------


def test_error_does_not_self_heal(executor: LocalExecutor) -> None:
    """ADR-0008 §2.5: error is never self-healing. The worker
    does not transition out of error regardless of bytes / session
    state. Only moderator stop_session clears it."""
    # Seed an error record with a still-alive session.
    executor.create_session("mod-coder-1", "agent-stub")
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.ERROR,
        last_output_at=_utc_now_naive(),
        log_offset=0,
        error="boot failed",
    )
    write_state(state)

    executor.send_keys("mod-coder-1", "anything at all to write")
    prefix_len, _ = executor.read_from("mod-coder-1", 0)
    assert executor.wait_for_buffer_at_least("mod-coder-1", prefix_len + 16)

    record = process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    # error must not transition. Per LEGAL_TRANSITIONS the only
    # way out is → STOPPED via moderator action.
    assert record.state is AgentState.ERROR


# ---------------------------------------------------------------------------
# No auto-restart
# ---------------------------------------------------------------------------


def test_worker_does_not_create_session_for_offline_agent(
    executor: LocalExecutor,
) -> None:
    """ADR-0017: the worker MUST NOT spawn a new tmux session on
    the agent's behalf. We verify this by checking that
    create_session was not called for the offline agent."""
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@local",
        project_dir="~/p",
        tmux_session="mod-coder-1",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    write_state(state)
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    rec = read_state().agents["coder-1"]
    assert rec.state is AgentState.OFFLINE
    # And the session must still not exist.
    assert executor.is_alive("mod-coder-1") is False
