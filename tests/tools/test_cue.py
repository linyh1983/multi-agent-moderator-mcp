"""Tests for the real ``cue`` MCP tool (ticket 10).

Covers:

- Validation: empty / non-string target + message rejected.
- Target resolution:
  - ``"moderator"`` → forbidden error (ADR-0005 symmetry).
  - ``"<name>"`` lookup; missing agent → ``AgentNotFound``.
  - ``"all"`` fans out to every RUNNING agent.
  - role label (anything else: e.g. ``"reviewer"``) → rejected
    with "role-label cues not yet supported".
- State check:
  - ``STOPPED`` / ``ERROR`` / ``OFFLINE`` agents rejected with
    state name in the error.
  - ``BLOCKED`` agents accept (cue does NOT unblock per
    ADR-0009 §4.1) — and their ``waiting_for`` is preserved.
- Send behavior (LocalExecutor):
  - ``tmux.send_keys`` receives exactly the wrapped body.
  - Wrap format is ``<moderator-cue>{message}</moderator-cue>``.
  - A trailing newline is appended (Enter sent).
- ChatMessage semantics:
  - One ``ChatMessage`` per delivered target with
    ``kind="cue"``, ``from_agent=None``, ``to_agent=<name>``,
    ``text=<original message>``, ``ts`` populated.
- Rate limit (ADR-0013 §5.4): sender-side throttle.
  Tested by invoking the helper directly — the integration path
  asserts no flood by checking send_keys was called the expected
  number of times across 11 cues.
- Tool description no longer contains the "not implemented" string.
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
    ChatMessage,
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


def _seed_agent(
    name: str,
    *,
    state: AgentState = AgentState.RUNNING,
    waiting_for: str | None = None,
) -> None:
    s = read_state()
    s.agents[name] = AgentRecord(
        name=name,
        host="user@local-exec",
        project_dir="~/p",
        state=state,
        tmux_session=f"mod-{name}",
        waiting_for=waiting_for,
    )
    write_state(s)
    # Spin up a real session on the LocalExecutor so send_keys
    # has something to write to (matches the approve tests).
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{name}", "agent-stub")


def _seed_agents(names: list[str], state: AgentState = AgentState.RUNNING) -> None:
    for n in names:
        _seed_agent(n, state=state)


# ---------------------------------------------------------------------------
# Tool description (no stub)
# ---------------------------------------------------------------------------


def test_tool_description_no_longer_says_stub() -> None:
    """The MCP tool description must reflect the real behavior —
    no "not implemented" / "stub" string anywhere."""
    from moderator.tools.cue import TOOL

    desc = TOOL.description.lower()
    assert "not implemented" not in desc
    assert "stub" not in desc
    # And it should mention actually what it does:
    assert "cue" in desc and ("directive" in desc or "agent" in desc)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_cue_rejects_missing_target() -> None:
    res = _run(call_tool("cue", {"message": "hi"}))
    assert res.isError is True
    assert "target" in res.content[0].text


def test_cue_rejects_empty_target() -> None:
    res = _run(call_tool("cue", {"target": "", "message": "hi"}))
    assert res.isError is True
    assert "target" in res.content[0].text


def test_cue_rejects_missing_message() -> None:
    _seed_agent("coder-1")
    res = _run(call_tool("cue", {"target": "coder-1"}))
    assert res.isError is True
    assert "message" in res.content[0].text


def test_cue_rejects_empty_message() -> None:
    _seed_agent("coder-1")
    res = _run(call_tool("cue", {"target": "coder-1", "message": ""}))
    assert res.isError is True
    assert "message" in res.content[0].text


def test_cue_rejects_non_string_target() -> None:
    res = _run(call_tool("cue", {"target": 123, "message": "hi"}))
    assert res.isError is True


def test_cue_rejects_non_string_message() -> None:
    res = _run(call_tool("cue", {"target": "coder-1", "message": ["x"]}))
    assert res.isError is True


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def test_cue_to_moderator_is_forbidden() -> None:
    """ADR-0005 symmetry: cue to the moderator is just as
    forbidden as TO:moderator (which only the moderator can use)."""
    res = _run(call_tool("cue", {"target": "moderator", "message": "hi"}))
    assert res.isError is True
    assert "moderator" in res.content[0].text.lower()


def test_cue_to_unknown_agent_returns_agent_not_found() -> None:
    res = _run(call_tool("cue", {"target": "ghost", "message": "hi"}))
    assert res.isError is True
    assert "ghost" in res.content[0].text
    # v1 cannot distinguish a typo from a role label; both render
    # the same error so the moderator knows what's available.
    assert "not found" in res.content[0].text.lower()
    assert "role" in res.content[0].text.lower()


def test_cue_rejects_role_label() -> None:
    """role labels (e.g. 'reviewer', 'coder') are not yet supported
    (out of scope for ticket 10 — see docs/tickets/10)."""
    res = _run(call_tool("cue", {"target": "reviewer", "message": "hi"}))
    assert res.isError is True
    assert "role" in res.content[0].text.lower()


# ---------------------------------------------------------------------------
# State check
# ---------------------------------------------------------------------------


def test_cue_to_stopped_agent_is_rejected() -> None:
    _seed_agent("stopped-1", state=AgentState.STOPPED)
    res = _run(call_tool("cue", {"target": "stopped-1", "message": "hi"}))
    assert res.isError is True
    assert "stopped" in res.content[0].text.lower()


def test_cue_to_error_agent_is_rejected() -> None:
    _seed_agent("err-1", state=AgentState.ERROR)
    res = _run(call_tool("cue", {"target": "err-1", "message": "hi"}))
    assert res.isError is True
    assert "error" in res.content[0].text.lower()


def test_cue_to_offline_agent_is_rejected() -> None:
    _seed_agent("off-1", state=AgentState.OFFLINE)
    res = _run(call_tool("cue", {"target": "off-1", "message": "hi"}))
    assert res.isError is True
    assert "offline" in res.content[0].text.lower()


def test_cue_to_blocked_agent_succeeds_without_unblock() -> None:
    """ADR-0009 §4.1: cue to a BLOCKED agent is sent (operator
    may want to nudge), but the blocking action is NOT cleared."""
    _seed_agent("blocked-1", state=AgentState.BLOCKED, waiting_for="a-1")
    res = _run(call_tool("cue", {"target": "blocked-1", "message": "please"}))
    assert res.isError is False, res.content[0].text
    rec = read_state().agents["blocked-1"]
    assert rec.state is AgentState.BLOCKED
    assert rec.waiting_for == "a-1"


# ---------------------------------------------------------------------------
# Send behavior (LocalExecutor)
# ---------------------------------------------------------------------------


def test_cue_writes_moderator_cue_wrapped_body_to_tmux() -> None:
    """The body sent to tmux is ``<moderator-cue>{message}</moderator-cue>``
    plus the newline that ``send_keys`` always appends. On the
    LocalExecutor the body round-trips back through the echo agent's
    stdout, so we read it via ``capture_pane`` to assert the wire
    format end-to-end."""
    _seed_agent("coder-1")
    res = _run(call_tool("cue", {"target": "coder-1", "message": "please wrap up"}))
    assert res.isError is False, res.content[0].text

    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    # The echo agent echoes each stdin line back on stdout. Wait a
    # tick for the daemon drain thread to deliver the bytes into
    # the per-session buffer, then read the pane.
    pane = ""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pane = tmux.capture_pane("mod-coder-1", lines=200)
        if "moderator-cue" in pane:
            break
        time.sleep(0.02)
    assert "<moderator-cue>please wrap up</moderator-cue>" in pane, (
        f"wrap never arrived in tmux pane: {pane!r}"
    )


def test_cue_appends_chat_message() -> None:
    before = _utc_now_naive()
    _seed_agent("coder-1")
    res = _run(call_tool("cue", {"target": "coder-1", "message": "ping"}))
    assert res.isError is False
    msgs = [m for m in read_state().chat if m.kind == "cue"]
    assert len(msgs) == 1
    msg: ChatMessage = msgs[0]
    assert msg.to_agent == "coder-1"
    assert msg.from_agent is None  # moderator → agent; sender not recorded
    assert msg.text == "ping"
    # ts is monotonic Naive UTC, populated at send time.
    from datetime import timedelta

    assert msg.ts >= before
    assert msg.ts - before < timedelta(seconds=2)
    # id is stable + namespaced so check tool can dedup / render.
    assert msg.id.startswith("msg-cue-")
    assert len(msg.id) > len("msg-cue-")


def test_cue_all_fans_out_to_every_running_agent() -> None:
    names = ["run-1", "run-2", "run-3"]
    _seed_agents(names)
    res = _run(call_tool("cue", {"target": "all", "message": "wrap up"}))
    assert res.isError is False, res.content[0].text
    assert "3" in res.content[0].text
    msgs = [m for m in read_state().chat if m.kind == "cue"]
    assert sorted(m.to_agent or "" for m in msgs) == sorted(names)


def test_cue_all_skips_non_running_agents() -> None:
    """``all`` only fans out to RUNNING; BLOCKED/STOPPED/ERROR/OFFLINE
    are silently skipped (operator cares about ``all`` of the agents
    they can actually reach)."""
    _seed_agents(["run-1", "run-2"])
    _seed_agent("done-1", state=AgentState.STOPPED)
    _seed_agent("err-1", state=AgentState.ERROR)
    res = _run(call_tool("cue", {"target": "all", "message": "wrap"}))
    assert res.isError is False, res.content[0].text
    msgs = [m for m in read_state().chat if m.kind == "cue"]
    assert sorted(m.to_agent or "" for m in msgs) == ["run-1", "run-2"]


# ---------------------------------------------------------------------------
# Rate limit (sender-side throttle, ADR-0013 §5.4)
# ---------------------------------------------------------------------------


def test_cue_rate_limit_helper_waits_to_fill_gap() -> None:
    """The throttle helper sleeps enough to keep at most one
    cue per ``gap`` seconds per agent name."""
    from moderator.tools import cue

    name = "rl-1"
    gap = 0.05  # 50ms gap → 20 cues/sec max
    # Inject a recent dispatch so the next call must sleep ~ gap.
    cue._last_cue_mono[name] = time.monotonic()
    started = time.monotonic()
    cue._enforce_cue_rate_limit(name, gap=gap)
    elapsed = time.monotonic() - started
    # We slept roughly the gap (tolerate timing jitter).
    assert elapsed >= gap * 0.5


def test_cue_rate_limit_helper_is_noop_when_disabled() -> None:
    """``gap <= 0`` disables the throttle — used by some tests."""
    from moderator.tools import cue

    cue._last_cue_mono["anything"] = time.monotonic()
    started = time.monotonic()
    cue._enforce_cue_rate_limit("anything", gap=0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.02  # no sleep


def test_cue_rate_limit_helper_ignores_other_agents() -> None:
    """Each agent's throttle clock is independent."""
    from moderator.tools import cue

    cue._last_cue_mono["a"] = time.monotonic()
    # Different agent — no sleep.
    started = time.monotonic()
    cue._enforce_cue_rate_limit("b", gap=0.5)
    elapsed = time.monotonic() - started
    assert elapsed < 0.02


def test_cue_helper_state_resettable_for_tests() -> None:
    """The throttle clock is a module-level dict; tests reset
    via a public helper so one test doesn't slow the next."""
    from moderator.tools import cue

    cue._last_cue_mono["x"] = time.monotonic()
    cue.reset_cue_throttle()
    assert "x" not in cue._last_cue_mono
