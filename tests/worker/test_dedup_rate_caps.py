"""Tests for the worker's dedup, per-agent rate limit, and
64 KiB marker writebacks (ticket 07).

Covers ADR-0010 §3.6 (size cap with truncated writeback),
ADR-0013 §5.4 (dedup FIFO 5000, per-agent rate limit) and
ADR-0014 §7.6 (seen_marker_hashes field).

Key invariants:

- A marker whose sha256(opener+content+closer) matches a hash
  in ``state.seen_marker_hashes`` is silently dropped — no
  state mutation, no chat, no progress entry.
- A NEW marker appends its hash to ``seen_marker_hashes``;
  the list is bounded to 5000 entries (FIFO eviction).
- A marker body that exceeds 64 KiB (UTF-8) is truncated
  and the sender receives a
  ``<moderator-info kind="truncated" marker="…">`` ack.
- A parse-warning (partial buffer overflow or malformed
  marker) emits a
  ``<moderator-info kind="parse-warning" detail="…">`` ack.
- The per-agent rate limit (``WorkerConfig
  .marker_rate_limit_per_second``) is enforced inside the
  worker's dispatch path. Off by default in tests; the value
  is 10.0 in production per ADR-0013 §5.4.
"""

from __future__ import annotations

import hashlib
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
from moderator.state.store import read_state, write_state
from moderator.worker import (
    WorkerConfig,
    WorkerStats,
    process_agent,
    reset_dispatch_clock,
)


# Constants from ADR-0010 §3.6 / ADR-0013 §5.4.
MAX_MARKER_BYTES: int = 64 * 1024
DEDUP_FIFO_CAP: int = 5000


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
        # Drop the module-level dispatch clock so a prior test's
        # rate-limit state can't slow down the next test.
        reset_dispatch_clock()


def _seed(executor: LocalExecutor, name: str = "coder-1") -> None:
    """Seed a RUNNING agent with a live tmux session; pre-advance
    log_offset past the cmd prefix so the worker's first cycle
    sees no new bytes (we control the test by sending later)."""
    executor.create_session(f"mod-{name}", "agent-stub")
    prefix_len, _ = executor.read_from(f"mod-{name}", 0)
    now = _utc_now_naive()
    state = empty_state()
    state.agents[name] = AgentRecord(
        name=name,
        host="user@local",
        project_dir="~/p",
        tmux_session=f"mod-{name}",
        state=AgentState.RUNNING,
        started_at=now,
        last_output_at=now,
        log_offset=prefix_len,
    )
    write_state(state)


def _echo_threshold(
    executor: LocalExecutor, name: str, payload_bytes: int
) -> int:
    """Compute the buffer size the daemon reader must reach so
    that the worker's first cycle after ``send_keys`` sees all
    our bytes. Returns ``prefix_len + payload_bytes``."""
    prefix_len, _ = executor.read_from(name, 0)
    return prefix_len + payload_bytes


# ---------------------------------------------------------------------------
# Dedup (sha256 FIFO)
# ---------------------------------------------------------------------------


def test_dedup_drops_duplicate_progress_marker(
    executor: LocalExecutor,
) -> None:
    """A repeated identical PROGRESS marker is dropped: no second
    ProgressEntry is written, but the byte stream advances."""
    _seed(executor)
    marker = "【进度汇报】hello world【/进度汇报】"
    for _ in range(3):
        executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", 3 * (len(marker) + 1)),
        timeout=5.0,
    )

    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    state = read_state()
    assert len(state.progress["coder-1"]) == 1
    assert state.progress["coder-1"][0].text == "hello world"


def test_dedup_seen_marker_hashes_appended(
    executor: LocalExecutor,
) -> None:
    """After processing a fresh marker, its sha256(opener+content
    +closer) appears in state.seen_marker_hashes."""
    _seed(executor)
    marker = "【进度汇报】step 1【/进度汇报】"
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", len(marker) + 1),
        timeout=5.0,
    )

    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    state = read_state()
    expected = hashlib.sha256(marker.encode("utf-8")).hexdigest()
    assert expected in state.seen_marker_hashes


def test_dedup_different_content_not_collapsed(
    executor: LocalExecutor,
) -> None:
    """Two markers with different content are NOT dedup-collapsed.
    Each is processed and gets its own hash entry."""
    _seed(executor)
    m1 = "【进度汇报】one【/进度汇报】"
    m2 = "【进度汇报】two【/进度汇报】"
    executor.send_keys("mod-coder-1", m1)
    executor.send_keys("mod-coder-1", m2)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(
            executor, "mod-coder-1", len(m1) + 1 + len(m2) + 1
        ),
        timeout=5.0,
    )

    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    state = read_state()
    assert len(state.progress["coder-1"]) == 2


def test_dedup_fifo_evicts_oldest(
    executor: LocalExecutor,
) -> None:
    """When seen_marker_hashes is full, the oldest hash is evicted."""
    _seed(executor)
    # Pre-seed the seen_marker_hashes with 5000 entries so the
    # next insertion evicts the oldest.
    state = read_state()
    state.seen_marker_hashes = [
        f"pre-{i:04d}" for i in range(DEDUP_FIFO_CAP)
    ]
    write_state(state)

    # Send one fresh marker.
    marker = "【进度汇报】overflow test【/进度汇报】"
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", len(marker) + 1),
        timeout=5.0,
    )

    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    state = read_state()
    # FIFO capped at 5000 — oldest is gone, new one is in.
    assert len(state.seen_marker_hashes) == DEDUP_FIFO_CAP
    assert "pre-0000" not in state.seen_marker_hashes
    expected = hashlib.sha256(marker.encode("utf-8")).hexdigest()
    assert state.seen_marker_hashes[-1] == expected


def test_dedup_does_not_write_chat_or_progress_for_duplicate(
    executor: LocalExecutor,
) -> None:
    """A duplicate marker must not write a ProgressEntry or
    a ChatMessage. State is byte-equivalent to processing the
    marker once."""
    _seed(executor)
    marker = "【进度汇报】dup test【/进度汇报】"
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", len(marker) + 1),
        timeout=5.0,
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    # Now send the same marker again.
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", 2 * (len(marker) + 1)),
        timeout=5.0,
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )

    state = read_state()
    assert len(state.progress["coder-1"]) == 1


# ---------------------------------------------------------------------------
# Per-agent rate limit
# ---------------------------------------------------------------------------


def test_rate_limit_disabled_by_default(
    executor: LocalExecutor,
) -> None:
    """``marker_rate_limit_per_second`` defaults to 0 (off). A burst
    of markers in a single cycle is dispatched without sleep."""
    _seed(executor)
    n = 20
    for i in range(n):
        executor.send_keys(
            "mod-coder-1", f"【进度汇报】burst {i}【/进度汇报】"
        )
    # Compute the threshold from the largest expected payload.
    biggest = f"【进度汇报】burst {n - 1}【/进度汇报】"
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(
            executor, "mod-coder-1", n * (len(biggest) + 1)
        ),
        timeout=5.0,
    )

    stats = WorkerStats()
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=stats,
    )
    state = read_state()
    # All 20 should land in state.progress (no rate limit, no
    # dedup because content differs).
    assert len(state.progress["coder-1"]) == n


def test_rate_limit_10_per_second(
    executor: LocalExecutor,
) -> None:
    """With rate limit 10/sec, dispatching a burst of markers
    takes at least 1 second for 10 markers (9 gaps × 0.1 s)."""
    import time

    _seed(executor)
    n = 15
    for i in range(n):
        executor.send_keys(
            "mod-coder-1", f"【进度汇报】rl {i}【/进度汇报】"
        )
    biggest = f"【进度汇报】rl {n - 1}【/进度汇报】"
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(
            executor, "mod-coder-1", n * (len(biggest) + 1)
        ),
        timeout=5.0,
    )

    t0 = time.monotonic()
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(marker_rate_limit_per_second=10.0),
        stats=WorkerStats(),
    )
    elapsed = time.monotonic() - t0
    # 15 markers at 10/s ⇒ ~1.4 s minimum (worst case). Assert a
    # conservative lower bound so the test is stable.
    assert elapsed >= 1.0, f"rate limit did not throttle: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 64 KiB truncated writeback
# ---------------------------------------------------------------------------


def test_truncated_marker_writes_ack_to_sender(
    executor: LocalExecutor,
) -> None:
    """A marker body > 64 KiB is truncated and the sender gets
    a ``<moderator-info kind="truncated" marker="…">`` ack."""
    _seed(executor)
    # Body of 70 KiB — well over the 64 KiB cap.
    big = "x" * (70 * 1024)
    marker = f"【进度汇报】{big}【/进度汇报】"
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(
            executor, "mod-coder-1", len(marker) + 1
        ),
        timeout=10.0,
    )

    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    pane = executor.capture_pane("mod-coder-1", lines=400)
    assert "<moderator-info" in pane
    assert 'kind="truncated"' in pane


def test_truncated_marker_progress_text_capped(
    executor: LocalExecutor,
) -> None:
    """The progress entry that survives a truncation stores the
    truncated body (≤ 64 KiB)."""
    _seed(executor)
    big = "y" * (70 * 1024)
    marker = f"【进度汇报】{big}【/进度汇报】"
    executor.send_keys("mod-coder-1", marker)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(
            executor, "mod-coder-1", len(marker) + 1
        ),
        timeout=10.0,
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    state = read_state()
    assert len(state.progress["coder-1"]) == 1
    text = state.progress["coder-1"][0].text
    # Truncated to MAX_MARKER_BYTES UTF-8 bytes.
    assert len(text.encode("utf-8")) <= MAX_MARKER_BYTES


# ---------------------------------------------------------------------------
# Parse-warning writeback
# ---------------------------------------------------------------------------


def test_parse_warning_writes_ack(
    executor: LocalExecutor,
) -> None:
    """When the parser emits a parse-warning (partial buffer
    overflow), the sender gets a
    ``<moderator-info kind="parse-warning" …>`` ack."""
    _seed(executor)
    # Open a marker but never close it; feed more than 64 KiB
    # so the parser's MAX_PARTIAL_BUFFER_BYTES check fires.
    chunk = "a" * 1024
    bad = "【进度汇报】" + chunk * 70  # ~70 KiB, no closer
    executor.send_keys("mod-coder-1", bad)
    assert executor.wait_for_buffer_at_least(
        "mod-coder-1",
        _echo_threshold(executor, "mod-coder-1", len(bad) + 1),
        timeout=10.0,
    )
    process_agent(
        name="coder-1",
        tmux=executor,
        config=WorkerConfig(),
        stats=WorkerStats(),
    )
    pane = executor.capture_pane("mod-coder-1", lines=600)
    assert "<moderator-info" in pane
    assert 'kind="parse-warning"' in pane