"""Tests for ticket 05 ``start_session`` extensions.

Covers ADR-0005 §6.5 (same-name reject) and the
``additional_agents`` allowlist wiring:

- ``start_session(name="A")`` succeeds and records
  ``additional_agents=[]`` by default.
- ``start_session(name="A", additional_agents=["B"])`` records
  ``additional_agents=["B"]`` on the AgentRecord.
- ``start_session(name="A")`` a second time returns
  ``AgentAlreadyExists(name="A", host=<existing>, state=<existing>)``
  — same name, any host, any state.
- Already-stopped agents also reject a same-name start (ADR-0005
  §6.5 — postmortem preserved).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.core.models import AgentRecord, AgentState, empty_state
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


def _seed(name: str, state: AgentState = AgentState.RUNNING) -> None:
    s = empty_state()
    s.agents[name] = AgentRecord(
        name=name,
        host="user@local-exec",
        project_dir="~/p",
        state=state,
        tmux_session=f"mod-{name}",
    )
    write_state(s)
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()
    tmux.create_session(f"mod-{name}", "agent-stub")


def test_start_session_succeeds_with_additional_agents() -> None:
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-A",
                "host": "user@local-exec",
                "project_dir": "~/p",
                "role_prompt": "You are coder A.",
                "additional_agents": ["coder-B"],
            },
        )
    )
    assert res.isError is False, res.content[0].text
    rec = read_state().agents["coder-A"]
    assert rec.additional_agents == ["coder-B"]


def test_start_session_default_additional_agents_empty() -> None:
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-X",
                "host": "user@local-exec",
                "project_dir": "~/p",
                "role_prompt": "You are coder X.",
            },
        )
    )
    assert res.isError is False, res.content[0].text
    rec = read_state().agents["coder-X"]
    assert rec.additional_agents == []


def test_start_session_same_name_running_rejected() -> None:
    _seed("coder-A", AgentState.RUNNING)
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-A",
                "host": "user@OTHER-host",
                "project_dir": "~/p",
                "role_prompt": "new",
            },
        )
    )
    assert res.isError is True
    text = res.content[0].text
    assert "AgentAlreadyExists" in text
    assert "coder-A" in text
    # The existing record must NOT have been overwritten.
    rec = read_state().agents["coder-A"]
    assert rec.host == "user@local-exec"


def test_start_session_same_name_stopped_rejected() -> None:
    """ADR-0005 §6.5: stopped records are preserved — re-using a
    name is still a conflict; the moderator must delete_record
    (v2) to free the name."""
    _seed("coder-A", AgentState.STOPPED)
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-A",
                "host": "user@local-exec",
                "project_dir": "~/p",
                "role_prompt": "new",
            },
        )
    )
    assert res.isError is True
    assert "AgentAlreadyExists" in res.content[0].text


def test_start_session_same_name_offline_rejected() -> None:
    _seed("coder-A", AgentState.OFFLINE)
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-A",
                "host": "user@local-exec",
                "project_dir": "~/p",
                "role_prompt": "new",
            },
        )
    )
    assert res.isError is True
    assert "AgentAlreadyExists" in res.content[0].text