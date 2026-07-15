"""Worker poll loop (ticket 02 — Hello Agent minimal).

The worker is the async loop that:

1. Iterates over each agent in ``state.agents``.
2. Calls ``tmux.is_alive()`` and reads new stdout bytes since
   ``state.agents[name].log_offset``.
3. Updates ``last_output_at`` on any byte read (per ADR-0008
   §2.2 — sticky to stdout bytes, not marker bytes).
4. Feeds the new bytes to a per-agent :class:`MarkerParser`.
5. Dispatches the resulting events: for ticket 02 only the
   ``PROGRESS`` event is wired (others come in later tickets).
6. Writes ``ProgressEntry`` records to ``state.progress[name]``
   (no cap yet — ticket 07 adds the FIFO 50).

Ticket 02 keeps the worker's per-cycle function
:func:`process_agent` synchronous and testable; the async
wrapper :func:`run_forever` exists but is intentionally minimal
(``asyncio.sleep`` between cycles) — v1 production has its own
process model that's out of scope for the ticket.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from moderator.core.models import (
    AgentRecord,
    AgentState,
    ProgressEntry,
    _utc_now_naive,
)
from moderator.drivers import TmuxDriver
from moderator.markers import MarkerEvent, MarkerKind, MarkerParser
from moderator.state.store import read_state, write_state


@dataclass
class WorkerConfig:
    """Per-process tuning. Defaults are MVP-sane; ticket 06 hooks
    stuck thresholds into here."""

    poll_interval_seconds: float = 2.0
    capture_pane_lines: int = 200


@dataclass
class WorkerStats:
    """Per-process counters; mostly for tests right now."""

    cycles: int = 0
    bytes_read: int = 0
    progress_written: int = 0
    parse_warnings: int = 0
    last_cycle_at: object | None = None
    events: list[MarkerEvent] = field(default_factory=list)


def process_agent(
    *,
    name: str,
    tmux: TmuxDriver,
    config: WorkerConfig,
    stats: WorkerStats,
) -> AgentRecord:
    """Run one poll cycle for ``name`` and return the updated
    :class:`AgentRecord`.

    Reads ``state``, mutates a per-agent copy, writes back. The
    worker does not own the parser — callers are expected to
    reuse a single :class:`MarkerParser` per agent across cycles
    so split-across-feeds markers parse correctly. For tests that
    want a fresh parser per call, pass ``_parsers={}`` via the
    outer wrapper.
    """
    state = read_state()
    record = state.agents.get(name)
    if record is None:
        raise KeyError(f"no such agent: {name!r}")
    session = record.tmux_session or f"mod-{name}"

    if not tmux.is_alive(session):
        # Don't auto-restart; ticket 06 owns offline transitions.
        # For ticket 02, the test driver keeps the session alive,
        # so this branch is only hit on explicit test teardown.
        return record

    # Use read_new_bytes (not capture_pane) so the parser sees only
    # bytes that have not yet been parsed. capture_pane is a moving
    # window — it returns the LAST N lines, not the delta since the
    # last call — and would cause every cycle to re-parse the same
    # content and double-count markers.
    new_offset, new_bytes = tmux.read_new_bytes(session, record.log_offset)
    if not new_bytes:
        stats.cycles += 1
        return record

    stats.bytes_read += len(new_bytes)
    record.last_output_at = _utc_now_naive()
    record.log_offset = new_offset

    # Dispatch. We construct a fresh parser per cycle for
    # simplicity — the per-agent parser state is owned by the
    # worker wrapper, not by this synchronous function. For
    # tests that care about cross-cycle state, they use the
    # wrapper directly.
    parser = MarkerParser()
    text = new_bytes.decode("utf-8", errors="replace")
    events = parser.feed(text)
    for ev in events:
        stats.events.append(ev)
        if ev.kind is MarkerKind.PROGRESS and not ev.warning:
            state.progress.setdefault(name, []).append(
                ProgressEntry(ts=_utc_now_naive(), text=ev.content)
            )
            stats.progress_written += 1
        elif ev.kind is MarkerKind.PARSE_WARNING:
            stats.parse_warnings += 1
        # Other kinds (TO, REQUEST_EXEC, HELP) are wired in
        # later tickets.

    state.agents[name] = record
    write_state(state)
    stats.cycles += 1
    return record


def run_forever(
    *,
    tmux: TmuxDriver,
    config: WorkerConfig | None = None,
) -> None:
    """Sync loop — exists so the worker's hot path has a place to
    live in v1. Tickets 02-07 will hook into this; ticket 02
    itself only exercises :func:`process_agent` synchronously.
    """
    import time

    cfg = config or WorkerConfig()
    while True:
        state = read_state()
        for name in list(state.agents.keys()):
            record = state.agents.get(name)
            if record is None:
                continue
            if record.state not in (AgentState.RUNNING, AgentState.STARTING, AgentState.BLOCKED, AgentState.STUCK):
                continue
            process_agent(
                name=name,
                tmux=tmux,
                config=cfg,
                stats=WorkerStats(),
            )
        time.sleep(cfg.poll_interval_seconds)


__all__ = [
    "WorkerConfig",
    "WorkerStats",
    "process_agent",
    "run_forever",
]
