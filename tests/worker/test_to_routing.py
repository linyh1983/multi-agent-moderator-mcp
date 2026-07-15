"""Tests for the worker's ``TO:`` marker routing (ticket 05).

Covers ADR-0005 (one-way allowlist, same-name reject) and
ADR-0010 (undelivered, parse-warning, nested-opener, ``TO:moderator``
ban):

- Happy path: A → B delivers the body to B's tmux stdin AND
  appends a peer ChatMessage to ``state.chat``.
- Target unknown (no AgentRecord): sender gets
  ``<moderator-info kind="parse-warning" detail="unknown peer X">``;
  no chat entry.
- Allowlist miss: B does not list A in ``additional_agents`` —
  same parse-warning ack; no chat entry.
- ``TO:moderator``: parse-warning ack (moderator-as-target
  forbidden); no chat entry.
- Target in offline/stopped/error: ``<moderator-info kind="undelivered"
  target="B">`` ack; chat entry written but metadata marked
  ``undelivered``.
- Nested opener inside a ``TO:`` body: inner opener ignored with
  a parse-warning; outer marker survives.
"""

from __future__ import annotations

import asyncio
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
from moderator.runtime import get_drivers, reset_drivers, set_drivers
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


def _seed_two_agents(
    sender: str = "coder-1",
    receiver: str = "coder-2",
    *,
    receiver_lists_sender: bool = True,
    receiver_state: AgentState = AgentState.RUNNING,
    receiver_session_alive: bool = True,
) -> None:
    state = empty_state()
    state.agents[sender] = AgentRecord(
        name=sender,
        host="user@local-exec",
        project_dir="~/p",
        state=AgentState.RUNNING,
        tmux_session=f"mod-{sender}",
        started_at=_utc_now_naive(),
        last_output_at=_utc_now_naive(),
        log_offset=0,
    )
    state.agents[receiver] = AgentRecord(
        name=receiver,
        host="user@local-exec",
        project_dir="~/p",
        state=receiver_state,
        tmux_session=f"mod-{receiver}",
        started_at=_utc_now_naive(),
        last_output_at=_utc_now_naive(),
        log_offset=0,
        additional_agents=[sender] if receiver_lists_sender else [],
    )
    write_state(state)

    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{sender}", "agent-stub")
    if receiver_session_alive:
        tmux.create_session(f"mod-{receiver}", "agent-stub")


def _send_marker(name: str, marker: str) -> None:
    """Write ``marker`` to the agent's tmux stdin (echoed back)."""
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.send_keys(f"mod-{name}", marker)
    assert isinstance(tmux, LocalExecutor)
    tmux.wait_for_buffer_at_least(
        f"mod-{name}", 64 + len(marker.encode("utf-8"))
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_to_marker_delivers_to_peer_tmux() -> None:
    _seed_two_agents()
    _send_marker("coder-1", "【TO:coder-2】hello peer【/TO:coder-2】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    # The echo agent of coder-2 should have echoed the message back.
    tmux.wait_for_buffer_at_least("mod-coder-2", 64)
    pane = tmux.capture_pane("mod-coder-2", lines=50)
    assert "hello peer" in pane


def test_to_marker_appends_chat_entry() -> None:
    _seed_two_agents()
    _send_marker("coder-1", "【TO:coder-2】hi there【/TO:coder-2】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    state = read_state()
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert len(peer_msgs) == 1
    msg = peer_msgs[0]
    assert msg.from_agent == "coder-1"
    assert msg.to_agent == "coder-2"
    assert msg.text == "hi there"


# ---------------------------------------------------------------------------
# Allowlist miss: receiver doesn't list sender → parse-warning ack
# ---------------------------------------------------------------------------


def test_to_marker_allowlist_miss_emits_parse_warning() -> None:
    _seed_two_agents(receiver_lists_sender=False)
    _send_marker(
        "coder-1", "【TO:coder-2】hello stranger【/TO:coder-2】"
    )
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    # Sender's tmux should have echoed the parse-warning ack.
    tmux.wait_for_buffer_at_least("mod-coder-1", 64)
    pane = tmux.capture_pane("mod-coder-1", lines=50)
    assert "<moderator-info" in pane
    assert 'kind="parse-warning"' in pane
    assert "coder-2" in pane


def test_to_marker_allowlist_miss_no_chat_entry() -> None:
    _seed_two_agents(receiver_lists_sender=False)
    _send_marker(
        "coder-1", "【TO:coder-2】hello stranger【/TO:coder-2】"
    )
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    state = read_state()
    # No peer chat entries (the message was rejected).
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert peer_msgs == []


# ---------------------------------------------------------------------------
# Unknown peer: no AgentRecord → parse-warning
# ---------------------------------------------------------------------------


def test_to_marker_unknown_peer_emits_parse_warning() -> None:
    _seed_two_agents(sender="coder-1", receiver="coder-2")
    _send_marker("coder-1", "【TO:coder-3】hi ghost【/TO:coder-3】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    tmux.wait_for_buffer_at_least("mod-coder-1", 64)
    pane = tmux.capture_pane("mod-coder-1", lines=50)
    assert "<moderator-info" in pane
    assert 'kind="parse-warning"' in pane
    assert "coder-3" in pane


def test_to_marker_unknown_peer_no_chat_entry() -> None:
    _seed_two_agents(sender="coder-1", receiver="coder-2")
    _send_marker("coder-1", "【TO:coder-3】hi ghost【/TO:coder-3】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    state = read_state()
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert peer_msgs == []


# ---------------------------------------------------------------------------
# TO:moderator is forbidden — agent must use 【求助人类】 instead
# ---------------------------------------------------------------------------


def test_to_moderator_emits_parse_warning() -> None:
    _seed_two_agents()
    _send_marker(
        "coder-1", "【TO:moderator】hi human【/TO:moderator】"
    )
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    tmux.wait_for_buffer_at_least("mod-coder-1", 64)
    pane = tmux.capture_pane("mod-coder-1", lines=50)
    assert "<moderator-info" in pane
    assert 'kind="parse-warning"' in pane


def test_to_moderator_no_chat_entry() -> None:
    _seed_two_agents()
    _send_marker(
        "coder-1", "【TO:moderator】hi human【/TO:moderator】"
    )
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    state = read_state()
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert peer_msgs == []


# ---------------------------------------------------------------------------
# Target in offline/stopped/error → undelivered ack, chat entry marked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_state",
    [AgentState.OFFLINE, AgentState.STOPPED, AgentState.ERROR],
)
def test_to_marker_target_offline_emits_undelivered(
    target_state: AgentState,
) -> None:
    _seed_two_agents(receiver_state=target_state, receiver_session_alive=False)
    _send_marker("coder-1", "【TO:coder-2】hi offline【/TO:coder-2】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    tmux.wait_for_buffer_at_least("mod-coder-1", 64)
    pane = tmux.capture_pane("mod-coder-1", lines=50)
    assert "<moderator-info" in pane
    assert 'kind="undelivered"' in pane
    assert "coder-2" in pane


def test_to_marker_target_offline_chat_marked_undelivered() -> None:
    _seed_two_agents(receiver_state=AgentState.OFFLINE, receiver_session_alive=False)
    _send_marker("coder-1", "【TO:coder-2】hi offline【/TO:coder-2】")
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    state = read_state()
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert len(peer_msgs) == 1
    msg = peer_msgs[0]
    assert msg.metadata is not None
    assert msg.metadata.get("undelivered") is True
    assert msg.metadata.get("undelivered_reason") == "offline"


# ---------------------------------------------------------------------------
# Nested opener inside a TO body — outer survives, inner ignored
# ---------------------------------------------------------------------------


def test_to_marker_nested_opener_is_ignored() -> None:
    """A nested 【进度汇报】 inside the TO body must not consume the
    outer closer. The outer marker must deliver normally."""
    _seed_two_agents()
    nested = (
        "【TO:coder-2】【进度汇报】nested【/进度汇报】 text"
        "【/TO:coder-2】"
    )
    _send_marker("coder-1", nested)
    _, tmux = get_drivers()
    process_agent(
        name="coder-1", tmux=tmux, config=WorkerConfig(), stats=WorkerStats()
    )

    # The TO marker delivered the full body (nested opener included
    # as plain text) to coder-2.
    tmux.wait_for_buffer_at_least("mod-coder-2", 64)
    pane = tmux.capture_pane("mod-coder-2", lines=50)
    assert "text" in pane
    assert "nested" in pane

    # A peer chat entry was written with the whole body (including
    # the nested opener as text). Per ADR-0010 nested openers are
    # treated as content.
    state = read_state()
    peer_msgs = [m for m in state.chat if m.kind == "peer"]
    assert len(peer_msgs) == 1
    assert "text" in peer_msgs[0].text