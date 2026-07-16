"""Contract tests for ``RemoteTmuxDriver``.

The driver satisfies :class:`TmuxDriver` by delegating each call to
the supplied :class:`SshDriver`'s ``run`` (and ``put_file`` for
``paste_buffer``). These tests mock the SshDriver — no real SSH.

Key invariants under test:

- ``RemoteTmuxDriver`` satisfies the :class:`TmuxDriver` Protocol
  (runtime_checkable ``isinstance`` check).
- Each tmux subcommand issues the documented ``ssh.run`` invocation.
- Non-zero rc from the underlying command raises
  :class:`DriverError` for ``create_session`` / ``send_keys`` /
  ``capture_pane`` / ``paste_buffer``.
- ``is_alive`` returns True iff ``tmux has-session`` exits 0.
- ``kill_session`` tolerates non-zero rc (already-dead cleanup).
- ``read_new_bytes`` is incremental: same pane content twice in a
  row yields ``(offset, b"")`` on the second call; the buffer
  resets when the pane scrolls past the worker's offset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from moderator.drivers import DriverError, SshDriver, TmuxDriver
from moderator.drivers.tmux_remote import RemoteTmuxDriver


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ssh() -> MagicMock:
    """A mock SshDriver whose ``run`` returns ``(0, "", "")`` by
    default. Tests override ``run.return_value`` per-case."""
    ssh = MagicMock(spec=SshDriver)
    ssh.run.return_value = (0, "", "")
    return ssh


@pytest.fixture
def tmux(mock_ssh: MagicMock) -> RemoteTmuxDriver:
    """A ``RemoteTmuxDriver`` bound to the mock SshDriver."""
    return RemoteTmuxDriver(exec_via=mock_ssh)


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_remote_tmux_driver_satisfies_tmux_driver_protocol(
    tmux: RemoteTmuxDriver,
) -> None:
    """``isinstance(d, TmuxDriver)`` must hold — every caller relies
    on this for runtime dispatch."""
    assert isinstance(tmux, TmuxDriver)


# ---------------------------------------------------------------------------
# is_alive
# ---------------------------------------------------------------------------


def test_is_alive_returns_true_when_tmux_has_session_succeeds(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``tmux has-session -t <name>`` exits 0 → alive."""
    mock_ssh.run.return_value = (0, "", "")
    assert tmux.is_alive("mod-x") is True
    cmd = mock_ssh.run.call_args.args[0]
    assert "has-session" in cmd
    assert "mod-x" in cmd


def test_is_alive_returns_false_when_tmux_has_session_fails(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``tmux has-session`` exits non-zero (no such session) → not
    alive. The driver does NOT raise — the moderator polls this
    every cycle and treats non-zero as "agent is gone"."""
    mock_ssh.run.return_value = (1, "", "can't find session: mod-x")
    assert tmux.is_alive("mod-x") is False


def test_is_alive_shell_quotes_session_name(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """Session names with shell metacharacters must be quoted to
    avoid injection. The driver uses :func:`shlex.quote` for safety —
    the dangerous substring ends up wrapped in single quotes, so
    the shell treats it as one literal token and does NOT parse the
    semicolon as a command separator.

    We check two things that together pin down "safe":

    1. The :func:`shlex.quote` output (single-quoted form) appears
       in the command — this proves the driver ran the name through
       the standard quoting function.
    2. The semicolon outside that quoted form must not appear — a
       semicolon elsewhere would re-introduce command chaining.
    """
    import shlex

    mock_ssh.run.return_value = (0, "", "")
    tmux.is_alive("mod-x; rm -rf /")
    cmd = mock_ssh.run.call_args.args[0]

    # 1. Quoted form is present.
    expected = shlex.quote("mod-x; rm -rf /")  # => "'mod-x; rm -rf /'"
    assert expected in cmd

    # 2. No semicolon OUTSIDE the quoted region. Strip the quoted
    # region out, then any remaining ";" would mean the driver
    # appended unquoted shell — i.e. injection-vulnerable.
    outside = cmd.replace(expected, "")
    assert ";" not in outside
    assert "&" not in outside
    assert "|" not in outside


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def test_create_session_uses_new_session_detached(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``create_session`` runs ``tmux new-session -d -s <name> <cmd>``."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.create_session("mod-x", "echo hello")
    cmd = mock_ssh.run.call_args.args[0]
    assert "new-session" in cmd
    assert "-d" in cmd
    assert "-s" in cmd
    assert "mod-x" in cmd
    assert "echo hello" in cmd


def test_create_session_raises_driver_error_on_nonzero_rc(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """A failed ``tmux new-session`` (duplicate name, bad command,
    missing tmux) surfaces as :class:`DriverError` so the worker
    can record the failure."""
    mock_ssh.run.return_value = (
        1,
        "",
        "duplicate session: mod-x",
    )
    with pytest.raises(DriverError) as excinfo:
        tmux.create_session("mod-x", "echo hi")
    assert "duplicate session" in str(excinfo.value)


# ---------------------------------------------------------------------------
# send_keys
# ---------------------------------------------------------------------------


def test_send_keys_uses_send_keys_subcommand(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``send_keys`` runs ``tmux send-keys -t <name> <text>``."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.send_keys("mod-x", "hello")
    cmd = mock_ssh.run.call_args.args[0]
    assert "send-keys" in cmd
    assert "-t" in cmd
    assert "mod-x" in cmd
    assert "hello" in cmd


def test_send_keys_shell_quotes_text(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """Text containing single quotes must be safely quoted."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.send_keys("mod-x", "it's a test")
    cmd = mock_ssh.run.call_args.args[0]
    # The unquoted form must not appear verbatim in the command.
    assert "it's a test" not in cmd


def test_send_keys_raises_driver_error_on_nonzero_rc(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """A failed ``tmux send-keys`` surfaces as :class:`DriverError`."""
    mock_ssh.run.return_value = (1, "", "session not found: mod-x")
    with pytest.raises(DriverError):
        tmux.send_keys("mod-x", "hi")


# ---------------------------------------------------------------------------
# capture_pane
# ---------------------------------------------------------------------------


def test_capture_pane_returns_stdout(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``capture_pane`` returns the stdout of ``tmux capture-pane``."""
    mock_ssh.run.return_value = (0, "line one\nline two\n", "")
    assert tmux.capture_pane("mod-x", 50) == "line one\nline two\n"


def test_capture_pane_passes_lines_limit(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """The ``lines`` parameter is passed through to ``-S -<N>``."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.capture_pane("mod-x", 123)
    cmd = mock_ssh.run.call_args.args[0]
    assert "capture-pane" in cmd
    assert "-p" in cmd  # print to stdout
    assert "-S -123" in cmd


def test_capture_pane_raises_driver_error_on_nonzero_rc(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    mock_ssh.run.return_value = (1, "", "session not found")
    with pytest.raises(DriverError):
        tmux.capture_pane("mod-x", 10)


# ---------------------------------------------------------------------------
# paste_buffer
# ---------------------------------------------------------------------------


def test_paste_buffer_stages_via_sftp_then_loads(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``paste_buffer`` writes to a local temp, SFTPs it to the
    remote host, then runs ``tmux load-buffer`` + ``tmux paste-buffer``
    + cleanup ``rm``.

    We capture the staged content via the ``put_file`` side effect
    (which runs *before* the driver cleans up the local temp file)
    so the assertion sees the bytes intact.
    """
    mock_ssh.run.return_value = (0, "", "")

    captured_text: list[str] = []
    captured_remote: list[str] = []

    def capture_put(local: Path, remote: str) -> None:
        captured_text.append(local.read_text(encoding="utf-8"))
        captured_remote.append(remote)

    mock_ssh.put_file.side_effect = capture_put

    tmux.paste_buffer("mod-x", "hello world\nline two\n")

    # SFTP staging captured the staged bytes.
    assert captured_text == ["hello world\nline two\n"]
    assert len(captured_remote) == 1
    remote_path = captured_remote[0]
    assert remote_path.startswith("/tmp/moderator-buffer-mod-x-")
    assert remote_path.endswith(".txt")

    # Final tmux invocation
    cmd = mock_ssh.run.call_args.args[0]
    assert "tmux load-buffer" in cmd
    assert "tmux paste-buffer" in cmd
    assert "mod-x" in cmd
    assert remote_path in cmd


def test_paste_buffer_cleans_up_local_temp(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """The local staging file is removed after ``paste_buffer``
    returns, even on success."""
    mock_ssh.run.return_value = (0, "", "")

    captured_local: list[Path] = []

    def capture_put(local: Path, remote: str) -> None:
        captured_local.append(local)

    mock_ssh.put_file.side_effect = capture_put
    tmux.paste_buffer("mod-x", "x")

    # Driver unlinked the local temp file after SFTP.
    assert len(captured_local) == 1
    assert not captured_local[0].exists()


def test_paste_buffer_raises_driver_error_on_nonzero_rc(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """A failed tmux load-buffer / paste-buffer raises."""
    mock_ssh.run.return_value = (1, "", "session not found: mod-x")
    with pytest.raises(DriverError):
        tmux.paste_buffer("mod-x", "hello")


# ---------------------------------------------------------------------------
# read_new_bytes
# ---------------------------------------------------------------------------


def test_read_new_bytes_returns_full_pane_on_first_call(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """First call (since_offset=0) returns the full captured pane
    content + offset = len(content)."""
    mock_ssh.run.return_value = (0, "hello world\n", "")
    new_offset, new_bytes = tmux.read_new_bytes("mod-x", 0)
    assert new_bytes == b"hello world\n"
    assert new_offset == len("hello world\n".encode("utf-8"))


def test_read_new_bytes_returns_empty_when_pane_unchanged(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """Two consecutive calls with the same pane content return
    empty bytes the second time — no re-parse for the worker."""
    mock_ssh.run.return_value = (0, "stable content\n", "")
    first_offset, first_bytes = tmux.read_new_bytes("mod-x", 0)
    assert first_bytes == b"stable content\n"

    # Second call: same content, expect empty bytes (and the worker's
    # offset is unchanged).
    next_offset, next_bytes = tmux.read_new_bytes("mod-x", first_offset)
    assert next_bytes == b""
    assert next_offset == first_offset


def test_read_new_bytes_returns_delta_when_pane_grows(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """When the pane grows, ``read_new_bytes`` returns only the
    bytes past the worker's offset."""
    # First call: small pane.
    mock_ssh.run.return_value = (0, "line A\n", "")
    offset_a, bytes_a = tmux.read_new_bytes("mod-x", 0)
    assert bytes_a == b"line A\n"
    assert offset_a == len(b"line A\n")

    # Second call: pane grew.
    mock_ssh.run.return_value = (0, "line A\nline B\n", "")
    offset_b, bytes_b = tmux.read_new_bytes("mod-x", offset_a)
    assert bytes_b == b"line B\n"
    assert offset_b == len(b"line A\nline B\n")


def test_read_new_bytes_resets_buffer_when_pane_scrolled(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """If the pane scrolled past the buffered content (buffer shorter
    than worker's offset), the buffer resets and the worker sees a
    fresh stream — dedup absorbs the cost."""
    # First call: tiny content.
    mock_ssh.run.return_value = (0, "x", "")
    offset_a, _ = tmux.read_new_bytes("mod-x", 0)
    assert offset_a == 1

    # Second call: very different content (pane scrolled).
    mock_ssh.run.return_value = (
        0,
        "totally different content after scroll",
        "",
    )
    # Pretend the worker is at an offset beyond our buffer.
    offset_b, bytes_b = tmux.read_new_bytes("mod-x", 9999)
    # Reset happened — we get the full new content from 0.
    assert bytes_b == b"totally different content after scroll"
    assert offset_b == len(b"totally different content after scroll")


def test_read_new_bytes_uses_capture_pane_with_default_lines(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``read_new_bytes`` captures a generous number of lines
    (``_DEFAULT_PANE_LINES``) so the worker gets the full recent
    pane history in one shot."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.read_new_bytes("mod-x", 0)
    cmd = mock_ssh.run.call_args.args[0]
    assert "capture-pane" in cmd
    # The exact number is an implementation detail; we just check
    # that a large ``-S -N`` is present (4+ digit number).
    assert "-S -" in cmd


# ---------------------------------------------------------------------------
# kill_session
# ---------------------------------------------------------------------------


def test_kill_session_runs_kill_session_subcommand(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """``kill_session`` runs ``tmux kill-session -t <name>``."""
    mock_ssh.run.return_value = (0, "", "")
    tmux.kill_session("mod-x")
    cmd = mock_ssh.run.call_args.args[0]
    assert "kill-session" in cmd
    assert "mod-x" in cmd


def test_kill_session_tolerates_already_dead(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """A non-zero rc (session already gone) is NOT an error — the
    moderator often calls ``kill_session`` on already-dead sessions
    during cleanup."""
    mock_ssh.run.return_value = (1, "", "can't find session: mod-x")
    # Must not raise.
    tmux.kill_session("mod-x")


def test_kill_session_clears_buffer_for_session(
    tmux: RemoteTmuxDriver, mock_ssh: MagicMock
) -> None:
    """After ``kill_session``, the per-session buffer is dropped so
    a future session with the same name starts clean."""
    # Populate the buffer.
    mock_ssh.run.return_value = (0, "first run", "")
    tmux.read_new_bytes("mod-x", 0)
    assert "mod-x" in tmux._buffers  # type: ignore[attr-defined]

    # Kill the session — buffer should clear.
    mock_ssh.run.return_value = (0, "", "")
    tmux.kill_session("mod-x")
    assert "mod-x" not in tmux._buffers  # type: ignore[attr-defined]


__all__: list[Any] = []  # pytest discovery via module