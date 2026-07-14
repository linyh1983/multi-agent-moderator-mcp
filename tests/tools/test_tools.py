"""Tests for the 6 MCP tool stubs (ticket 01).

Acceptance covered:
- 6 tools visible to the MCP layer (start_session, check, approve,
  reject, cue, stop_session).
- ``check()`` against empty state returns a one-line response
  referencing empty state.
- ``start_session(...)`` against a fake host returns a clean error
  string (no stacktrace) AND an error-state record on disk.
- Missing-args path for ``start_session`` returns a clean error too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from moderator.state.store import read_state
from moderator.tools import call_tool, list_tool_specs


# All handlers are async; pytest-asyncio isn't installed (per ticket 01
# "minimal deps"), so drive them via asyncio.run explicitly.


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a clean, per-test state file."""
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))


# ---------------------------------------------------------------------------
# Registry shape — the 6 tools are visible
# ---------------------------------------------------------------------------


def test_list_tool_specs_exposes_six_tools_in_canonical_order() -> None:
    names = [t.name for t in list_tool_specs()]
    assert names == [
        "start_session",
        "check",
        "approve",
        "reject",
        "cue",
        "stop_session",
    ]


def test_each_tool_definition_has_name_description_and_schema() -> None:
    for tool in list_tool_specs():
        assert tool.name
        assert tool.description
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"


def test_call_tool_unknown_name_returns_error() -> None:
    result = _run(call_tool("nope", {}))
    assert result.isError is True
    assert "unknown tool" in result.content[0].text


# ---------------------------------------------------------------------------
# check() — empty-state one-liner is the ticket 01 acceptance line
# ---------------------------------------------------------------------------


def test_check_against_empty_state_returns_empty_state_one_liner() -> None:
    result = _run(call_tool("check", {}))
    assert result.isError is False
    assert result.content[0].text == "(no agents; use start_session to begin)"


def test_check_against_state_with_agents_mentions_them() -> None:
    from moderator.core.models import AgentRecord, AgentState
    from moderator.state.store import write_state
    from moderator.core.models import empty_state

    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@fake-host.example",
        project_dir="~/proj",
        state=AgentState.RUNNING,
    )
    write_state(state)

    result = _run(call_tool("check", {}))
    assert result.isError is False
    assert "coder-1" in result.content[0].text


# ---------------------------------------------------------------------------
# start_session() — missing args, clean error, no stacktrace
# ---------------------------------------------------------------------------


def test_start_session_missing_required_args_returns_clean_error() -> None:
    result = _run(call_tool("start_session", {"name": "only-name"}))
    assert result.isError is True
    assert "missing required args" in result.content[0].text
    assert "Traceback" not in result.content[0].text


# ---------------------------------------------------------------------------
# start_session() — fake host, clean error + error-state record on disk
# ---------------------------------------------------------------------------


def test_start_session_against_fake_host_returns_clean_error() -> None:
    result = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-1",
                "host": "user@fake-host.example",
                "project_dir": "~/proj",
                "role_prompt": "you are a coder",
            },
        )
    )
    assert result.isError is True
    text = result.content[0].text
    assert "stub driver" in text
    assert "Traceback" not in text


def test_start_session_against_fake_host_writes_error_record_to_disk() -> None:
    """Acceptance: ``start_session`` against a fake host writes an
    error-state record on disk afterwards."""
    _run(
        call_tool(
            "start_session",
            {
                "name": "coder-1",
                "host": "user@fake-host.example",
                "project_dir": "~/proj",
                "role_prompt": "you are a coder",
            },
        )
    )

    state = read_state()
    assert "coder-1" in state.agents
    record = state.agents["coder-1"]
    assert record.state.value == "error"
    assert record.error is not None
    assert "stub driver" in record.error


# ---------------------------------------------------------------------------
# Stubs that explicitly defer to later tickets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, args",
    [
        ("approve", {"action_ids": ["a-1"]}),
        ("reject", {"action_ids": ["a-1"], "reason": "nope"}),
        ("cue", {"target": "coder-1", "message": "ping"}),
        ("stop_session", {"name": "coder-1"}),
    ],
)
def test_stub_tools_return_not_implemented(tool: str, args: dict) -> None:
    result = _run(call_tool(tool, args))
    assert result.isError is True
    text = result.content[0].text
    assert "not implemented" in text
    assert "Traceback" not in text