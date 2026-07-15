"""Worker poll loop (ticket 02 — Hello Agent minimal; tickets 04 + 05
extended it).

The worker is the async loop that:

1. Iterates over each agent in ``state.agents``.
2. Calls ``tmux.is_alive()`` and reads new stdout bytes since
   ``state.agents[name].log_offset``.
3. Updates ``last_output_at`` on any byte read (per ADR-0008
   §2.2 — sticky to stdout bytes, not marker bytes).
4. Feeds the new bytes to a per-agent :class:`MarkerParser`.
5. Dispatches the resulting events: PROGRESS (ticket 02),
   REQUEST_EXEC (ticket 04), and TO (ticket 05) are wired; HELP
   lands in a later ticket.
6. Writes ``ProgressEntry`` records to ``state.progress[name]``
   (no cap yet — ticket 07 adds the FIFO 50).

Ticket 02 keeps the worker's per-cycle function
:func:`process_agent` synchronous and testable; the async
wrapper :func:`run_forever` exists but is intentionally minimal
(``asyncio.sleep`` between cycles) — v1 production has its own
process model that's out of scope for the ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from moderator.core.lifecycle import transition
from moderator.core.models import (
    AgentRecord,
    AgentState,
    ApprovalAction,
    ChatMessage,
    ModeratorState,
    ProgressEntry,
    _utc_now_naive,
)
from moderator.drivers import DriverError, TmuxDriver
from moderator.markers import MarkerEvent, MarkerKind, MarkerParser
from moderator.state.store import read_state, write_state


# Reserved target name. Per ADR-0005 §"TO:moderator 禁" the
# moderator is never a TO: recipient — agents must use the
# 【求助人类】 marker instead. Catches typos that would otherwise
# silently drop the message.
_FORBIDDEN_TO_TARGET: str = "moderator"


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
    to_delivered: int = 0
    to_parse_warnings: int = 0
    to_undelivered: int = 0
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
        elif ev.kind is MarkerKind.REQUEST_EXEC and not ev.warning:
            # Dispatch may have replaced ``state.agents[name]``
            # with a new record (e.g. running → blocked). Re-bind
            # ``record`` so the writeback at the end uses the
            # up-to-date copy.
            record = _handle_request_exec(
                state, record, ev.content, session, tmux
            )
        elif ev.kind is MarkerKind.TO and not ev.warning:
            record = _handle_to_marker(
                state, record, ev.target, ev.content, session, tmux, stats
            )
        # HELP lands in a later ticket.

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


def _handle_request_exec(
    state: ModeratorState,
    record: AgentRecord,
    content: str,
    session: str,
    tmux: TmuxDriver,
) -> AgentRecord:
    """Materialize a REQUEST_EXEC marker into a pending action.

    Per ADR-0007 §6.1: every REQUEST_EXEC gets a writeback ack
    ``<moderator-info action-id="a-N" status="queued">``. Per
    ADR-0009 §4.1: if the agent was running, transition to blocked
    and record ``waiting_for`` (single-wait invariant). If the
    agent was already blocked on another action, do NOT re-touch
    ``waiting_for`` — the new action is queued in state.actions
    but the agent keeps waiting on the first one.

    Returns the (possibly updated) record so the caller can
    re-bind ``state.agents[name]`` with the new copy.
    """
    state.action_seq += 1
    seq = state.action_seq
    aid = f"a-{seq}"
    action = ApprovalAction(
        id=aid,
        seq=seq,
        agent_name=record.name,
        content=content,
        status="pending",
        created_at=_utc_now_naive(),
    )
    state.actions[aid] = action

    if record.state is AgentState.RUNNING:
        # Pure transition: blocked replaces waiting_for with the
        # first action. waiting_for assignment happens below.
        new_record = transition(record, AgentState.BLOCKED)
        new_record.waiting_for = aid
        state.agents[record.name] = new_record
        record = new_record
    elif record.state is AgentState.BLOCKED:
        # Single-wait invariant: do NOT re-transition and do NOT
        # overwrite waiting_for. The new action accumulates in
        # state.actions.
        pass
    else:
        # Other states (STARTING/STUCK/OFFLINE/STOPPED/ERROR) are
        # not legal origins for a new request. We still record
        # the action so the moderator sees it (audit), but we
        # don't move the agent. v1 ticket 06+ may refine this.
        pass

    # Send ack writeback. Driver errors are swallowed — the action
    # is already persisted in state.actions.
    ack = f'<moderator-info action-id="{aid}" status="queued">'
    try:
        tmux.send_keys(session, ack)
    except DriverError:
        pass

    return record


def _send_ack_safely(session: str, ack: str, tmux: TmuxDriver) -> None:
    """Writeback an info tag to the sender's tmux. Driver errors
    are swallowed — the ack is informational; the canonical
    record lives in ``state``."""
    try:
        tmux.send_keys(session, ack)
    except DriverError:
        pass


def _parse_warning_ack(detail: str) -> str:
    """Compose a parse-warning writeback tag (ADR-0010)."""
    # The detail may include spaces; we keep the tag minimal —
    # detail text only — so the agent sees a clear, parseable
    # reason without overflowing its UI.
    safe = detail.replace('"', "'")
    return f'<moderator-info kind="parse-warning" detail="{safe}">'


def _undelivered_ack(target: str) -> str:
    """Compose an undelivered writeback tag (ADR-0010)."""
    return f'<moderator-info kind="undelivered" target="{target}">'


def _handle_to_marker(
    state: ModeratorState,
    record: AgentRecord,
    target: str | None,
    content: str,
    session: str,
    tmux: TmuxDriver,
    stats: WorkerStats,
) -> AgentRecord:
    """Route a parsed ``TO:<target>`` marker to a peer.

    Per ADR-0005 §6.2 (one-way allowlist) and ADR-0010 (acks,
    undelivered, parse-warning, TO:moderator ban):

    - ``TO:moderator`` is forbidden; sender gets a parse-warning
      ack and no chat entry. (Use 【求助人类】 instead.)
    - Unknown peer (no ``AgentRecord`` in state) → parse-warning
      ack mentioning the target.
    - Allowlist miss: ``target not in state.agents[sender]
      .additional_agents`` → parse-warning ack. No chat entry.
    - Target in OFFLINE / STOPPED / ERROR → undelivered ack; a
      peer chat entry is still written, marked undelivered via
      ``ChatMessage.metadata`` so downstream tools can see the
      attempt.
    - Happy path: deliver the body to the peer's tmux stdin AND
      append a peer :class:`ChatMessage` to ``state.chat``.

    Returns the (unchanged) record — TO does not touch lifecycle.
    """
    if target is None:
        # Parser invariant violation: TO without a target name
        # should never reach the dispatcher. Be defensive: emit
        # a parse-warning and skip.
        stats.to_parse_warnings += 1
        _send_ack_safely(
            session, _parse_warning_ack("malformed TO marker"), tmux
        )
        return record

    if target == _FORBIDDEN_TO_TARGET:
        stats.to_parse_warnings += 1
        _send_ack_safely(
            session,
            _parse_warning_ack(
                "TO:moderator is forbidden; use 【求助人类】 instead"
            ),
            tmux,
        )
        return record

    target_record = state.agents.get(target)
    if target_record is None:
        stats.to_parse_warnings += 1
        _send_ack_safely(
            session,
            _parse_warning_ack(f"unknown peer {target!r}"),
            tmux,
        )
        return record

    # Allowlist check (ADR-0005 §6.2): one-way. The *recipient*
    # must list the *sender* in its additional_agents allowlist.
    if record.name not in target_record.additional_agents:
        stats.to_parse_warnings += 1
        _send_ack_safely(
            session,
            _parse_warning_ack(
                f"target {target!r} does not accept messages from "
                f"{record.name!r} (allowlist miss)"
            ),
            tmux,
        )
        return record

    # Determine whether the target can actually receive bytes
    # right now. OFFLINE / STOPPED / ERROR all mean the tmux
    # session is not (reliably) alive, so we mark undelivered.
    is_live_state = target_record.state in (
        AgentState.RUNNING,
        AgentState.STARTING,
        AgentState.BLOCKED,
        AgentState.STUCK,
    )
    target_session = target_record.tmux_session or f"mod-{target}"
    session_alive = is_live_state and tmux.is_alive(target_session)

    # Append a peer chat entry FIRST. The chat log is the
    # authoritative record; tmux delivery is best-effort.
    now = _utc_now_naive()
    chat_meta: dict[str, str | bool] | None = None
    if not session_alive:
        chat_meta = {
            "undelivered": True,
            "undelivered_reason": target_record.state.value,
        }

    state.chat.append(
        ChatMessage(
            id=f"msg-peer-{uuid4().hex[:12]}",
            ts=now,
            kind="peer",
            from_agent=record.name,
            to_agent=target,
            text=content,
            metadata=chat_meta,
        )
    )

    if not session_alive:
        stats.to_undelivered += 1
        _send_ack_safely(session, _undelivered_ack(target), tmux)
        return record

    # Happy path: deliver to the peer's tmux stdin. We send the
    # body followed by a newline so the agent sees a complete
    # input. Driver errors collapse to an undelivered ack so the
    # sender is not left in the dark.
    try:
        tmux.send_keys(target_session, content)
    except DriverError:
        stats.to_undelivered += 1
        # Mark the chat entry we just appended as undelivered.
        state.chat[-1] = state.chat[-1].model_copy(
            update={
                "metadata": {
                    "undelivered": True,
                    "undelivered_reason": "send_keys failed",
                }
            }
        )
        _send_ack_safely(session, _undelivered_ack(target), tmux)
        return record

    stats.to_delivered += 1
    return record


__all__ = [
    "WorkerConfig",
    "WorkerStats",
    "process_agent",
    "run_forever",
]
