"""Regression test for ticket 13 / bug B4.

The original ``_build_agent_command`` returned
``claude --role-file <path>`` — but ``--role-file`` is NOT a Claude
Code CLI flag (verified via ``claude --help`` on the dev host on
2026-07-16: ``error: unknown option '--role-file'``). The agent
exits ``rc=1`` immediately, tmux destroys the session, and the
next ``paste_buffer`` step fails with a misleading
``can't find pane: mod-...`` error.

This module locks in two contracts:

1. ``_build_agent_command`` returns a string containing the real
   Claude Code flag ``--system-prompt-file`` and NOT the invented
   ``--role-file``.
2. End-to-end, ``start_session`` against a recording tmux driver
   captures a ``create_session`` command that uses the real flag.

Both would have caught B4 instantly — it was a string-level bug,
no paramiko/tmux round-trip required for #1.

See ``docs/tickets/13-agent-cli-flag.md`` (to be created) and
``tmp_manual/TEST_PLAN.md`` §"B4" for the bug report.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from moderator.drivers import SshDriver, TmuxDriver
from moderator.runtime import reset_drivers, set_drivers
from moderator.tools import call_tool
from moderator.tools.start_session import _build_agent_command


# ---------------------------------------------------------------------------
# Pure unit test — no driver injection needed
# ---------------------------------------------------------------------------


def test_build_agent_command_uses_system_prompt_file_flag() -> None:
    """``_build_agent_command`` must return a string containing the
    real Claude Code flag ``--system-prompt-file`` and NOT the
    invented ``--role-file``. ticket 13 / bug B4."""
    cmd = _build_agent_command("/tmp/moderator/role-x.txt", "/tmp")
    assert "--system-prompt-file" in cmd, (
        f"expected --system-prompt-file flag; got {cmd!r}. "
        f"ticket 13 / bug B4: agent CLI flag mismatch."
    )
    assert "--role-file" not in cmd, (
        f"--role-file is not a Claude Code CLI flag; got {cmd!r}. "
        f"ticket 13 / bug B4: agent CLI flag mismatch."
    )


def test_build_agent_command_includes_role_path() -> None:
    """The command must reference the role prompt's remote path so
    the agent process can read its system prompt on startup."""
    cmd = _build_agent_command("/tmp/moderator/role-coder.txt", "/tmp")
    assert "/tmp/moderator/role-coder.txt" in cmd, (
        f"role path missing from command; got {cmd!r}"
    )


def test_build_agent_command_quotes_project_dir() -> None:
    """``project_dir`` is shell-quoted so paths with spaces survive.
    The ``cd`` prefix must remain in place so the agent runs in
    the user's intended working directory."""
    cmd = _build_agent_command(
        "/tmp/moderator/role-x.txt", "/tmp/my project dir"
    )
    # ``shlex.quote`` wraps the path in single quotes when it
    # contains whitespace; the literal substring must appear.
    assert "/tmp/my project dir" in cmd or "'/tmp/my project dir'" in cmd, (
        f"project_dir path missing from cd-prefix; got {cmd!r}"
    )
    assert cmd.startswith("cd "), f"command must start with cd; got {cmd!r}"


# ---------------------------------------------------------------------------
# Integration-style test — driver injection captures the actual
# command string handed to tmux.create_session
# ---------------------------------------------------------------------------


class _RecordingSshDriver:
    """Records SSH method calls; satisfies SshDriver by duck type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def connect(self, host: str | None = None) -> None:
        self.calls.append(("connect", (host,)))

    def put_file(self, local: Path, remote: str) -> None:
        self.calls.append(("put_file", (str(local), remote)))

    def run(self, command: str) -> tuple[int, str, str]:
        self.calls.append(("run", (command,)))
        return 0, "", ""

    def close(self) -> None:
        self.calls.append(("close", ()))


class _RecordingTmux:
    """Captures the ``command`` argument passed to ``create_session``
    so the test can assert the assembled agent command uses the
    real Claude Code flag end-to-end."""

    def __init__(self) -> None:
        self.create_session_calls: list[tuple[str, str]] = []
        self.is_alive_after_create: bool = True

    def is_alive(self, session: str) -> bool:
        return self.is_alive_after_create

    def create_session(self, session: str, command: str) -> None:
        self.create_session_calls.append((session, command))

    def send_keys(self, session: str, text: str) -> None:
        pass

    def paste_buffer(self, session: str, text: str) -> None:
        pass

    def capture_pane(self, session: str, lines: int) -> str:
        return ""

    def read_new_bytes(self, session: str, since_offset: int) -> tuple[int, bytes]:
        return since_offset, b""

    def kill_session(self, session: str) -> None:
        pass


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    monkeypatch.setenv("MODERATOR_DRIVER", "ssh")
    ssh = _RecordingSshDriver()
    tmux = _RecordingTmux()
    set_drivers(ssh, tmux)  # type: ignore[arg-type]
    yield
    reset_drivers()


def test_start_session_invokes_tmux_create_with_system_prompt_file_command() -> None:
    """End-to-end: when ``start_session`` runs against a recording
    tmux driver, the ``command`` argument handed to
    ``tmux.create_session`` must use ``--system-prompt-file`` and
    NOT ``--role-file``. ticket 13 / bug B4."""
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-e2e",
                "host": "django-app-openeuler-service-10",
                "project_dir": "/tmp",
                "role_prompt": "you are a coder",
            },
        )
    )
    assert res.isError is False, (
        f"start_session must succeed against recording drivers; "
        f"got error: {res.content[0].text!r}"
    )

    # Re-acquire the tmux driver the fixture installed.
    from moderator.runtime import get_drivers

    _ssh, tmux = get_drivers()
    assert len(tmux.create_session_calls) == 1, (
        f"expected exactly one create_session call; "
        f"got {tmux.create_session_calls!r}"
    )
    session, command = tmux.create_session_calls[0]
    assert session == "mod-coder-e2e", (
        f"unexpected session name; got {session!r}"
    )
    assert "--system-prompt-file" in command, (
        f"tmux create_session command must use --system-prompt-file; "
        f"got {command!r}. ticket 13 / bug B4."
    )
    assert "--role-file" not in command, (
        f"tmux create_session command must NOT use --role-file "
        f"(not a real Claude Code flag); got {command!r}. "
        f"ticket 13 / bug B4."
    )
    # Sanity-check: role path and cd prefix are preserved.
    assert "/tmp/moderator/role-coder-e2e.txt" in command, (
        f"role path missing from tmux command; got {command!r}"
    )
    assert command.startswith("cd "), (
        f"tmux command must start with cd; got {command!r}"
    )