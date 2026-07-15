"""Tests for the real ``check()`` rendering (ticket 03).

Coverage (per docs/tickets/03-hello-agent-end-to-end.md):

- 4 sections rendered in the priority order
  ``## Urgent help → ## Pending approvals → ## Agents → ## Recent chat``.
- Each agent row shows name, host, state, last-output age, progress
  count + tail (1-3 lines).
- Agents in ``stuck`` / ``offline`` / ``error`` are visually flagged.
- When the state file's ``mtime`` differs from the last-seen
  ``mtime`` in this process, ``check()`` prepends an external-change
  warning.
- ``only_urgent`` restricts the view to help_requests.
- ``max_chat_lines`` caps the chat tail.
- The progress tail is capped at 3 lines regardless of history.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from moderator.core.models import (
    AgentRecord,
    AgentState,
    ApprovalAction,
    ChatMessage,
    HelpRequest,
    ProgressEntry,
    _utc_now_naive,
    empty_state,
)
from moderator.runtime import reset_drivers
from moderator.state.store import read_state, write_state
from moderator.tools import call_tool
from moderator.tools.check import reset_mtime_cache


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    # Reset module-level mtime cache so each test starts from a clean
    # "no prior read" state.
    reset_mtime_cache()
    yield
    reset_mtime_cache()
    reset_drivers()


# ---------------------------------------------------------------------------
# Section ordering — the 4 sections appear in the priority order
# ---------------------------------------------------------------------------


def _seed_full_state() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive(),
    )
    state.agents["reviewer-1"] = AgentRecord(
        name="reviewer-1",
        host="user@beta",
        project_dir="~/p",
        state=AgentState.STUCK,
        last_output_at=_utc_now_naive() - timedelta(minutes=10),
    )
    state.progress["coder-1"] = [
        ProgressEntry(ts=_utc_now_naive(), text="wrote a.py"),
        ProgressEntry(ts=_utc_now_naive(), text="ran pytest"),
    ]
    state.actions["a-1"] = ApprovalAction(
        id="a-1",
        seq=1,
        agent_name="coder-1",
        content="git push",
        status="pending",
        created_at=_utc_now_naive(),
    )
    state.actions["a-2"] = ApprovalAction(
        id="a-2",
        seq=2,
        agent_name="reviewer-1",
        content="npm install",
        status="approved",
        created_at=_utc_now_naive(),
        decided_at=_utc_now_naive(),
    )
    state.help_requests.append(
        HelpRequest(
            id="h-1",
            agent_name="reviewer-1",
            content="what's the deploy command?",
            ts=_utc_now_naive(),
        )
    )
    state.chat.append(
        ChatMessage(
            id="c-1",
            ts=_utc_now_naive(),
            kind="peer",
            from_agent="coder-1",
            to_agent="reviewer-1",
            text="please review",
        )
    )
    write_state(state)


def test_check_renders_four_sections_in_priority_order() -> None:
    _seed_full_state()
    out = _run(call_tool("check", {})).content[0].text

    idx_help = out.find("## Urgent help")
    idx_appr = out.find("## Pending approvals")
    idx_agents = out.find("## Agents")
    idx_chat = out.find("## Recent chat")
    assert -1 < idx_help < idx_appr < idx_agents < idx_chat


def test_check_help_section_appears_when_help_requests_exist() -> None:
    _seed_full_state()
    out = _run(call_tool("check", {})).content[0].text
    help_block = out[out.find("## Urgent help") : out.find("## Pending approvals")]
    assert "reviewer-1" in help_block
    assert "what's the deploy command?" in help_block


def test_check_pending_approvals_section_lists_only_pending() -> None:
    _seed_full_state()
    out = _run(call_tool("check", {})).content[0].text
    appr_block = out[out.find("## Pending approvals") : out.find("## Agents")]
    assert "a-1" in appr_block
    assert "a-2" not in appr_block  # approved, not pending


def test_check_no_help_section_when_no_help_requests() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    assert "## Urgent help" not in out


def test_check_no_pending_section_when_no_pending_actions() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    assert "## Pending approvals" not in out


def test_check_agents_section_lists_all_agents() -> None:
    _seed_full_state()
    out = _run(call_tool("check", {})).content[0].text
    agents_block = out[out.find("## Agents") : out.find("## Recent chat")]
    assert "coder-1" in agents_block
    assert "reviewer-1" in agents_block


# ---------------------------------------------------------------------------
# Agent row content — name, host, state, last-output age, progress count + tail
# ---------------------------------------------------------------------------


def test_check_agent_row_includes_name_host_state() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha.example",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    assert "coder-1" in out
    assert "user@alpha.example" in out
    assert "running" in out


def test_check_agent_row_includes_last_output_age() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
        last_output_at=_utc_now_naive() - timedelta(seconds=42),
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    # Age is rendered in seconds/minutes; we don't pin the exact unit,
    # but the number 42 (or close to it) should appear.
    assert "42" in out or "40" in out or "43" in out


def test_check_agent_row_includes_progress_count_and_tail() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    state.progress["coder-1"] = [
        ProgressEntry(ts=_utc_now_naive(), text="wrote a.py"),
        ProgressEntry(ts=_utc_now_naive(), text="ran pytest"),
        ProgressEntry(ts=_utc_now_naive(), text="opened PR"),
    ]
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    # The "3" count and at least one of the progress lines should appear.
    assert "3" in out
    assert "wrote a.py" in out or "ran pytest" in out or "opened PR" in out


def test_check_progress_tail_capped_at_3_lines() -> None:
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    state.progress["coder-1"] = [
        ProgressEntry(ts=_utc_now_naive(), text=f"line-{i}") for i in range(20)
    ]
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    # The 3 most-recent entries should be present in the tail. The
    # tail is the last 3 entries joined with " | ", so the rendered
    # substring is exact: "line-17 | line-18 | line-19".
    assert "line-17 | line-18 | line-19" in out
    # Older entries should not appear (their digits don't appear in
    # the tail). We check by looking for "line-0" / "line-1" with a
    # pipe or end-of-line after the digit.
    assert "line-0 |" not in out
    assert "line-0\n" not in out
    assert "line-16 |" not in out


# ---------------------------------------------------------------------------
# Visual flag for stuck / offline / error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state_value", ["stuck", "offline", "error"])
def test_check_flags_stuck_offline_error_agents(state_value: str) -> None:
    state = empty_state()
    state.agents["a-bad"] = AgentRecord(
        name="a-bad",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState(state_value),
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    # The flag must be present on the agent line. We don't pin the
    # exact glyph (text-based UI per ADR-0008 §consequences) but we
    # require SOME marker that distinguishes a-bad from a healthy
    # agent. The current contract: lines for bad states start with ⚠.
    agents_block = out[out.find("## Agents") :]
    bad_line = next(
        (ln for ln in agents_block.splitlines() if "a-bad" in ln), ""
    )
    assert bad_line, "agent row missing"
    assert "⚠" in bad_line or "[!]" in bad_line or "*" in bad_line


def test_check_running_agent_not_flagged() -> None:
    state = empty_state()
    state.agents["a-good"] = AgentRecord(
        name="a-good",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    agents_block = out[out.find("## Agents") :]
    good_line = next(
        (ln for ln in agents_block.splitlines() if "a-good" in ln), ""
    )
    assert good_line
    assert "⚠" not in good_line and "[!]" not in good_line


# ---------------------------------------------------------------------------
# mtime external-change warning (ADR-0011)
# ---------------------------------------------------------------------------


def test_check_no_mtime_warning_on_first_call() -> None:
    """First call in a process has no prior mtime to compare against,
    so it never warns. Establishes the baseline for the next tests."""
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    out = _run(call_tool("check", {})).content[0].text
    assert "⚠" not in out
    assert "State changed externally" not in out


def test_check_no_mtime_warning_when_state_unchanged() -> None:
    """Two consecutive checks with no external write must not warn."""
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    _run(call_tool("check", {}))
    out2 = _run(call_tool("check", {})).content[0].text
    assert "State changed externally" not in out2


def test_check_mtime_warning_when_state_changed_externally(
    tmp_path: Path,
) -> None:
    """If a different process (or the moderator manually) writes the
    state file between checks, the next check must warn."""
    state_path = Path(
        __import__("os").environ["MODERATOR_STATE_PATH"]
    )
    state = empty_state()
    state.agents["coder-1"] = AgentRecord(
        name="coder-1",
        host="user@alpha",
        project_dir="~/p",
        state=AgentState.RUNNING,
    )
    write_state(state)

    # First check: establishes the baseline mtime.
    _run(call_tool("check", {}))

    # Simulate an external write: bump mtime into the future and
    # rewrite the file with the same content.
    future = time.time() + 5
    state2 = read_state()
    state2.agents["coder-1"].last_output_at = _utc_now_naive()
    write_state(state2)
    # Force a measurable mtime change even on filesystems with second
    # resolution.
    import os

    os.utime(state_path, (future, future))

    out = _run(call_tool("check", {})).content[0].text
    assert "State changed externally" in out


# ---------------------------------------------------------------------------
# only_urgent — restrict to help_requests
# ---------------------------------------------------------------------------


def test_check_only_urgent_renders_only_help_section() -> None:
    _seed_full_state()
    out = _run(call_tool("check", {"only_urgent": True})).content[0].text
    assert "## Urgent help" in out
    assert "## Pending approvals" not in out
    assert "## Agents" not in out
    assert "## Recent chat" not in out


# ---------------------------------------------------------------------------
# max_chat_lines — cap the chat tail
# ---------------------------------------------------------------------------


def test_check_max_chat_lines_caps_chat_section() -> None:
    state = empty_state()
    for i in range(50):
        state.chat.append(
            ChatMessage(
                id=f"c-{i}",
                ts=_utc_now_naive(),
                kind="peer",
                from_agent="coder-1",
                to_agent="reviewer-1",
                text=f"msg-{i}",
            )
        )
    write_state(state)

    out_default = _run(call_tool("check", {})).content[0].text
    out_capped = _run(
        call_tool("check", {"max_chat_lines": 5})
    ).content[0].text
    # The default already caps; a 5-line cap should produce a strictly
    # shorter chat block than the default (20).
    chat_default_len = len(
        out_default[out_default.find("## Recent chat") :]
    )
    chat_capped_len = len(
        out_capped[out_capped.find("## Recent chat") :]
    )
    assert chat_capped_len < chat_default_len
