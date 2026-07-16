"""Tests for the 6 MCP tools.

Covers tickets 01 + 02:

- 6 tools visible to the MCP layer (start_session, check, approve,
  reject, cue, stop_session).
- ``check()`` against empty state returns a one-line response
  referencing empty state.
- ``start_session(...)`` missing-args path returns a clean error.
- ``start_session(...)`` happy path (LocalExecutor) transitions
  STARTING → RUNNING within one call, writes role_prompt_path and
  tmux_session fields, and persists the role prompt to the
  simulated remote.
- ``start_session(...)`` against an unsafe name returns a clean
  error and writes an error-state record.
- 4 stubs (approve, reject, cue, stop_session) return
  "not implemented" with no stacktrace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.drivers.local import LocalExecutor
from moderator.runtime import reset_drivers, set_drivers
from moderator.state.store import read_state
from moderator.tools import call_tool, list_tool_specs


# All handlers are async; pytest-asyncio isn't installed (per ticket 01
# "minimal deps"), so drive them via asyncio.run explicitly.


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Each test starts from a clean, per-test state file AND a clean,
    per-test driver pair so concurrent tests don't share a workdir."""
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    workdir = tmp_path / "local-exec"
    workdir.mkdir(parents=True, exist_ok=True)
    execu = LocalExecutor(workdir=workdir)
    set_drivers(execu, execu)
    yield
    # Tear down: kill any spawned sessions, drop the module cache so
    # the next test sees a fresh default.
    try:
        execu.close()
    except Exception:
        pass
    reset_drivers()


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


def test_check_against_empty_state_mentions_no_agents() -> None:
    """Ticket 03: check() now renders a real 4-section snapshot.
    The empty-state contract is "no agents" appears in the
    ``## Agents`` section so the moderator knows what to do next."""
    result = _run(call_tool("check", {}))
    assert result.isError is False
    text = result.content[0].text
    assert "## Agents" in text
    assert "no agents" in text
    assert "start_session" in text


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
# start_session() — happy path with LocalExecutor
# ---------------------------------------------------------------------------


def test_start_session_happy_path_transitions_to_running() -> None:
    """LocalExecutor-backed happy path: a successful call flips the
    agent to RUNNING and records role_prompt_path + tmux_session."""
    result = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-1",
                "host": "user@local-exec",
                "project_dir": "~/proj",
                "role_prompt": "you are a coder",
            },
        )
    )
    assert result.isError is False, result.content[0].text
    state = read_state()
    record = state.agents["coder-1"]
    assert record.state.value == "running"
    assert record.role_prompt_path == "/tmp/moderator/role-coder-1.txt"
    assert record.tmux_session == "mod-coder-1"
    assert record.started_at is not None
    assert record.last_output_at is not None


def test_start_session_happy_path_persists_role_prompt_remotely(
    tmp_path: Path,
) -> None:
    """The role prompt must land on the simulated remote at the
    documented path — that's the artifact the agent process will read."""
    _run(
        call_tool(
            "start_session",
            {
                "name": "coder-2",
                "host": "user@local-exec",
                "project_dir": "~/proj",
                "role_prompt": "you are a reviewer",
            },
        )
    )
    workdir = tmp_path / "local-exec"
    staged = workdir / "tmp" / "moderator" / "role-coder-2.txt"
    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == "you are a reviewer"


def test_start_session_role_prompt_never_persisted_to_state_json() -> None:
    """Per ADR-0003 the role prompt text is never written into
    state.json — only the path is recorded."""
    _run(
        call_tool(
            "start_session",
            {
                "name": "coder-3",
                "host": "user@local-exec",
                "project_dir": "~/proj",
                "role_prompt": "TOP-SECRET role prompt 12345",
            },
        )
    )
    raw = Path(__file__).parent.parent.parent / "moderator_state.json"
    # We rely on MODERATOR_STATE_PATH having been monkey-patched to a
    # tmp_path file. Resolve from the autouse fixture's view of
    # MODERATOR_STATE_PATH via the env-var path the test owns.
    import os

    state_file = Path(os.environ["MODERATOR_STATE_PATH"])
    raw_text = state_file.read_text(encoding="utf-8")
    assert "TOP-SECRET" not in raw_text
    assert "you are a coder" not in raw_text  # also no leftover from prior tests


# ---------------------------------------------------------------------------
# start_session() — unsafe name: clean error + error-state record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "has spaces",
        "with/slash",
        "with;semicolon",
        "with$dollar",
        "",
    ],
)
def test_start_session_unsafe_name_returns_clean_error(name: str) -> None:
    result = _run(
        call_tool(
            "start_session",
            {
                "name": name,
                "host": "user@local-exec",
                "project_dir": "~/proj",
                "role_prompt": "x",
            },
        )
    )
    assert result.isError is True
    assert "Traceback" not in result.content[0].text
    # Empty name is caught by the "missing required args" path; other
    # unsafe names hit the safe-name validator.
    if name:
        assert "name must match" in result.content[0].text


# ---------------------------------------------------------------------------
# Stubs that explicitly defer to later tickets
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stubs that explicitly defer to later tickets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, args",
    [
        # ticket 04 made approve/reject real; ticket 10 made
        # cue real. Without seeded state they should each return
        # a clean error (no traceback) describing WHY, rather than
        # blanket "not implemented".
        ("approve", {"action_ids": ["a-1"]}),
        ("reject", {"action_ids": ["a-1"], "reason": "nope"}),
        ("cue", {"target": "coder-1", "message": "ping"}),
    ],
)
def test_stub_tools_return_clean_error_when_state_empty(tool: str, args: dict) -> None:
    result = _run(call_tool(tool, args))
    assert result.isError is True
    text = result.content[0].text
    assert "Traceback" not in text
    # Real validation message — names the tool and the specific
    # reason. (Replaces the old "not implemented" assertion once
    # the cue stub landed in ticket 10.)
    assert tool in text


def test_stop_session_against_empty_state_returns_clean_error() -> None:
    """stop_session is no longer a stub (ticket 03). It returns a
    clean error when the agent doesn't exist rather than crashing
    or saying 'not implemented'."""
    result = _run(call_tool("stop_session", {"name": "coder-1"}))
    assert result.isError is True
    text = result.content[0].text
    assert "no such agent" in text
    assert "Traceback" not in text