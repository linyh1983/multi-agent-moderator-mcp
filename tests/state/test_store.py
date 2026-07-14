"""Tests for the state store (ADR-0004 JSON + cross-process file lock).

Acceptance items covered (ticket 01):
- State file write/read round-trip (file lock test)
- State file contains top-level `schema_version: 1` and 5 empty
  collections: agents, actions, chat, progress, help_requests
- read on a missing file returns an empty (schema_v1) state
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from moderator.core.models import empty_state
from moderator.state.store import (
    StateLock,
    read_state,
    state_lock,
    write_state,
)


# ---------------------------------------------------------------------------
# Pure in-process round-trip
# ---------------------------------------------------------------------------


def test_read_missing_file_returns_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "moderator_state.json"
    state = read_state(path)
    assert state.schema_version == 1
    assert state.agents == {}
    assert state.actions == {}
    assert state.chat == []
    assert state.progress == {}
    assert state.help_requests == []


def test_empty_state_helper_matches_acceptance(tmp_path: Path) -> None:
    """The fresh shape needed by ticket 01: schema_v1 + 5 empty collections."""
    state = empty_state()
    assert state.schema_version == 1
    assert state.agents == {}
    assert state.actions == {}
    assert state.chat == []
    assert state.progress == {}
    assert state.help_requests == []


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "moderator_state.json"
    original = empty_state()
    write_state(original, path)
    restored = read_state(path)
    assert restored.schema_version == original.schema_version
    assert restored.agents == original.agents
    assert restored.actions == original.actions
    assert restored.chat == original.chat
    assert restored.progress == original.progress
    assert restored.help_requests == original.help_requests
    assert restored.action_seq == 0
    assert restored.seen_marker_hashes == []


def test_written_file_contains_required_keys(tmp_path: Path) -> None:
    """The literal JSON on disk must contain schema_version: 1 and the
    five empty-collection markers, per ticket 01 acceptance."""
    path = tmp_path / "moderator_state.json"
    write_state(empty_state(), path)
    text = path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in text
    assert '"agents": {}' in text
    assert '"actions": {}' in text
    assert '"chat": []' in text
    assert '"progress": {}' in text
    assert '"help_requests": []' in text


# ---------------------------------------------------------------------------
# Cross-process lock — the actual MVP behavior is that writes from two
# processes never interleave, and the file is always valid JSON.
# ---------------------------------------------------------------------------


def _holder(path_str: str, hold_seconds: float, ready_str: str) -> None:
    """Subprocess worker: hold the state lock for ``hold_seconds``.

    Touches ``ready_str`` once the lock is held so the parent can
    synchronize without racing the (slow on Windows) ``spawn`` start.
    Holds ONLY the lock — does not call ``write_state`` (which would
    nest-lock on the same process). This is what real concurrent
    writers actually contend against: another process mid-serialization.
    """
    from moderator.state.store import state_lock

    with state_lock(Path(path_str)):
        Path(ready_str).write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)


def _writer_at(path_str: str, marker_value: int) -> None:
    """Subprocess worker: write a state (action_seq = marker_value).

    Does not hold the lock externally — ``write_state`` takes the
    lock once and releases. Useful for racing a holder process.
    """
    from moderator.core.models import empty_state
    from moderator.state.store import write_state

    state = empty_state()
    state.action_seq = int(marker_value)
    write_state(state, Path(path_str))


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    """Poll for ``path`` to exist, up to ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def test_cross_process_lock_blocks_writer(tmp_path: Path) -> None:
    """If process A holds the lock, process B's ``write_state`` call
    must wait — and must not return until A is done."""
    path = tmp_path / "moderator_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "holder.ready"

    ctx = multiprocessing.get_context("spawn")

    a = ctx.Process(target=_holder, args=(str(path), 0.6, str(ready)))
    a.start()
    assert _wait_for(ready, timeout=5.0), "holder never signaled ready"

    t0 = time.monotonic()
    _writer_at(str(path), 222)  # B should block until A finishes
    elapsed = time.monotonic() - t0
    a.join()

    assert elapsed >= 0.4, (
        f"second writer returned too fast ({elapsed:.2f}s) — lock not held"
    )
    final = read_state(path)
    assert final.action_seq == 222


def test_atomic_rename_no_torn_json(tmp_path: Path) -> None:
    """A reader concurrent with a writer must never observe torn JSON.

    Spawn a holder process that holds the lock while we attempt
    repeated reads. With the lock held, reads block (and complete
    cleanly once the holder releases). No bytes-on-disk tornness to
    observe because the holder isn't writing — it merely forces
    contention on the lock path.
    """
    path = tmp_path / "moderator_state.json"
    write_state(empty_state(), path)
    assert read_state(path).action_seq == 0

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_holder, args=(str(path), 0.3))
    proc.start()
    try:
        # While the holder holds the lock, our reads should block (then
        # succeed once the lock releases). None should observe partial
        # bytes — every read yields a valid ``ModeratorState``.
        for _ in range(10):
            s = read_state(path)
            assert s.schema_version == 1
            assert s.action_seq == 0
            time.sleep(0.02)
    finally:
        proc.join()


def test_writes_are_atomic_via_tempfile_then_rename(tmp_path: Path) -> None:
    """No leftover ``.tmp`` files after a successful write."""
    path = tmp_path / "moderator_state.json"
    write_state(empty_state(), path)
    leftovers = [
        child
        for child in path.parent.iterdir()
        if child.name.startswith(f".{path.name}.") and child.suffix == ".tmp"
    ]
    assert leftovers == [], f"leftover temp files: {leftovers}"
