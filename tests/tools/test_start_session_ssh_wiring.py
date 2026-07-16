"""Regression test for ticket 12 / bug B1.

The runtime hands ``ParamikoSshDriver`` to ``start_session._start_one``
in lazy mode (no host). The original ticket 09 commit assumed the
runtime would call ``ssh.connect(host)`` somewhere; that call site
never landed, so ``put_file`` failed with ``not connected (called
after close)``.

This test locks in the contract: when the runtime drives
``start_session`` against an SSH driver pair, ``connect(host)`` must
be invoked on the SSH driver before the first ``put_file`` or
``run`` call, and the host argument must equal the per-agent host
passed to ``start_session``.

Throwaway integration with the real ParamikoSshDriver is not
required — a thin ``RecordingSshDriver`` that satisfies the
SshDriver Protocol and captures the method-call order is enough
to lock the contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from moderator.drivers import SshDriver, TmuxDriver
from moderator.runtime import reset_drivers, set_drivers
from moderator.state.store import read_state
from moderator.tools import call_tool


class RecordingSshDriver:
    """Records every method call so tests can assert ordering.

    Satisfies :class:`SshDriver` Protocol at the duck-type level
    (we don't need runtime_checkable; we just need the methods the
    driver Protocol declares). tmux operations are stubbed — we
    are not exercising the tmux path here, just the SSH wiring.
    """

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


class _StubTmux:
    def is_alive(self, session: str) -> bool:
        return True

    def create_session(self, session: str, command: str) -> None:
        pass

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
    ssh = RecordingSshDriver()
    tmux = _StubTmux()
    set_drivers(ssh, tmux)  # type: ignore[arg-type]
    yield
    reset_drivers()


def test_start_session_via_ssh_invokes_connect_before_put_file() -> None:
    """The SSH driver's ``connect(host)`` must be called BEFORE
    ``put_file`` so the lazy driver is wired up before any network
    call. Without this, ``put_file`` raises "not connected" and
    start_session fails end-to-end (bug B1)."""
    res = _run(
        call_tool(
            "start_session",
            {
                "name": "coder-e2e",
                "host": "django-app-openeuler-service-10",
                "project_dir": "~/proj",
                "role_prompt": "you are a coder",
            },
        )
    )
    assert res.isError is False, (
        f"start_session must succeed; got error: {res.content[0].text!r}"
    )

    # Re-acquire the same driver the fixture installed — the runtime
    # uses get_drivers() which returns whatever set_drivers put in.
    from moderator.runtime import get_drivers

    ssh, _ = get_drivers()
    names = [c[0] for c in ssh.calls]
    assert "connect" in names, (
        f"connect(host) was never called — calls={ssh.calls!r}"
    )
    # First call must be connect (host=p), not put_file.
    assert names[0] == "connect", (
        f"connect must be the first SSH call; got {names!r}"
    )
    # And connect was passed the per-agent host.
    connect_call = next(c for c in ssh.calls if c[0] == "connect")
    assert connect_call[1] == ("django-app-openeuler-service-10",), (
        f"connect must receive the per-agent host; got {connect_call!r}"
    )
    # And a put_file followed.
    assert "put_file" in names, f"put_file never happened: {ssh.calls!r}"

    # State should record the running agent.
    rec = read_state().agents["coder-e2e"]
    assert rec.state.value == "running"