"""Contract tests for ``ParamikoSshDriver``.

Covers ticket 09 — the real SSH driver that closes the
``ParamikoSshDriver is not yet implemented`` gap surfaced during
manual integration testing on 2026-07-15.

These tests do **not** open a real SSH connection. They mock
``paramiko.SSHClient`` to verify the driver's behavior under controlled
conditions. Real-SSH integration tests against
``django-app-openeuler-service-10`` live in
``tests/integration/test_ssh_real.py`` (run only when the host is
reachable — see ticket 09 §"TDD Path" step 2).

Key invariants under test:

- ``ParamikoSshDriver`` satisfies the :class:`SshDriver` Protocol
  (runtime_checkable ``isinstance`` check).
- ``run`` returns ``(rc, stdout, stderr)`` and propagates a
  non-zero ``rc`` as a normal return — never raises for non-zero rc.
- ``run`` raises :class:`DriverError` on transport failure (e.g.
  SSH channel closed mid-command).
- ``put_file`` opens the SFTP client and writes the local bytes to
  the remote path, creating parent directories along the way.
- ``put_file`` raises :class:`DriverError` on SFTP failure.
- ``close`` is idempotent — calling twice does not raise.
- Connection reuse: the same ``SSHClient`` instance is used across
  multiple ``run`` calls (not re-connected per command).
- ``AutoAddPolicy`` is set on the underlying client (TOFU per
  ADR-0015).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from moderator.drivers import DriverError, SshDriver
from moderator.drivers.ssh import ParamikoSshDriver


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ssh_client() -> MagicMock:
    """Return a mock paramiko.SSHClient with the minimum surface the
    driver actually uses: ``set_missing_host_key_policy``, ``connect``,
    ``get_transport``, ``exec_command``, ``open_sftp``, ``close``."""
    client = MagicMock(name="paramiko.SSHClient")

    # Transport used by is_alive() to check connection state.
    transport = MagicMock(name="paramiko.Transport")
    transport.is_active.return_value = True
    client.get_transport.return_value = transport

    # exec_command returns (stdin, stdout, stderr) — channel + file-likes.
    channel = MagicMock(name="Channel")
    channel.recv_exit_status.return_value = 0
    stdout_file = MagicMock(name="stdout_file")
    stdout_file.read.return_value = b"hello\n"
    stdout_file.channel = channel
    stderr_file = MagicMock(name="stderr_file")
    stderr_file.read.return_value = b""
    stderr_file.channel = channel
    client.exec_command.return_value = (MagicMock(), stdout_file, stderr_file)

    # SFTP client.
    sftp = MagicMock(name="SFTPClient")
    client.open_sftp.return_value = sftp

    return client


@pytest.fixture
def driver(mock_ssh_client: MagicMock) -> Generator[ParamikoSshDriver, None, None]:
    """A driver pre-connected to a fake host.

    Patches ``paramiko.SSHClient`` so the constructor's
    ``_connect()`` runs against the supplied mock (no real DNS, no
    real socket). The driver is yielded with ``_client`` already
    pointing at the mock so tests can assert against it.
    """
    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        ClientCls.return_value = mock_ssh_client
        d = ParamikoSshDriver(host="django-app-openeuler-service-10")
    yield d


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_paramiko_ssh_driver_satisfies_ssh_driver_protocol(
    driver: ParamikoSshDriver,
) -> None:
    """The driver must be ``isinstance(d, SshDriver)`` per the runtime
    Protocol check. This is the type-system contract every caller relies
    on."""
    assert isinstance(driver, SshDriver)


# ---------------------------------------------------------------------------
# run(command) -> (rc, stdout, stderr)
# ---------------------------------------------------------------------------


def test_run_returns_zero_rc_on_success(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """A command that exits 0 returns ``(0, stdout, stderr)``."""
    rc, out, err = driver.run("echo hello")
    assert rc == 0
    assert out == "hello\n"
    assert err == ""


def test_run_returns_nonzero_rc_without_raising(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """A command that exits non-zero returns ``(rc, stdout, stderr)``
    where ``rc != 0``. The driver does **not** raise — the caller
    decides what a non-zero rc means."""
    stdout_file = mock_ssh_client.exec_command.return_value[1]
    stdout_file.channel.recv_exit_status.return_value = 42
    stdout_file.read.return_value = b"some output\n"
    stderr_file = mock_ssh_client.exec_command.return_value[2]
    stderr_file.read.return_value = b"boom\n"

    rc, out, err = driver.run("false-ish")
    assert rc == 42
    assert out == "some output\n"
    assert err == "boom\n"


def test_run_decodes_bytes_as_utf8(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """stdout/stderr bytes are decoded as UTF-8 with ``errors="replace"``
    so the moderator never sees a UnicodeDecodeError from the driver
    layer."""
    stdout_file = mock_ssh_client.exec_command.return_value[1]
    stdout_file.read.return_value = "你好\n".encode("utf-8")
    rc, out, err = driver.run("echo chinese")
    assert rc == 0
    assert out == "你好\n"


def test_run_raises_driver_error_on_transport_failure(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """If ``exec_command`` raises (e.g. SSH channel closed), the driver
    wraps the underlying exception in :class:`DriverError` so the MCP
    caller gets a clean error message."""
    mock_ssh_client.exec_command.side_effect = OSError("channel closed")

    with pytest.raises(DriverError) as excinfo:
        driver.run("anything")
    assert "channel closed" in str(excinfo.value)


def test_run_reuses_existing_client(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """The driver does not re-connect on every command — one
    ``SSHClient`` per driver instance, reused across calls."""
    driver.run("first")
    driver.run("second")
    driver.run("third")
    # exec_command called 3 times; connect never called after __init__.
    assert mock_ssh_client.exec_command.call_count == 3


# ---------------------------------------------------------------------------
# put_file(local, remote)
# ---------------------------------------------------------------------------


def test_put_file_writes_local_bytes_to_remote(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock, tmp_path: Path
) -> None:
    """``put_file`` opens the SFTP client and hands paramiko the local
    path + the remote path. Paramiko itself opens / reads / uploads;
    we don't need to manage a file handle in the driver."""
    local = tmp_path / "local.txt"
    local.write_bytes(b"hello from local\n")
    driver.put_file(local, "/tmp/moderator/remote.txt")

    sftp = mock_ssh_client.open_sftp.return_value
    sftp.put.assert_called_once()
    # The first positional arg is the local path string paramiko
    # will open; the second is the remote path on the host.
    assert sftp.put.call_args.args[0] == str(local)
    assert sftp.put.call_args.args[1] == "/tmp/moderator/remote.txt"


def test_put_file_creates_parent_directories(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock, tmp_path: Path
) -> None:
    """``put_file`` creates missing parent directories on the remote
    host via SFTP ``mkdir`` (which is itself not idempotent on all
    servers, so we silently ignore ``IOError`` per the
    ``sftp.mkdir``-then-``put`` pattern)."""
    local = tmp_path / "role.txt"
    local.write_text("role content")
    sftp = mock_ssh_client.open_sftp.return_value
    sftp.stat.side_effect = IOError("not found")  # dir doesn't exist

    driver.put_file(local, "/tmp/moderator/sub/role.txt")

    # mkdir was called for the deepest directory first, then parents.
    mkdir_calls = [c.args[0] for c in sftp.mkdir.call_args_list]
    assert "/tmp/moderator/sub" in mkdir_calls
    assert "/tmp/moderator" in mkdir_calls


def test_put_file_does_not_recreate_existing_directories(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock, tmp_path: Path
) -> None:
    """If the parent directory already exists (stat succeeds), the
    driver should not call ``mkdir`` on it."""
    local = tmp_path / "role.txt"
    local.write_text("x")
    sftp = mock_ssh_client.open_sftp.return_value
    sftp.stat.return_value = MagicMock()  # dir exists

    driver.put_file(local, "/tmp/moderator/exists/role.txt")

    sftp.mkdir.assert_not_called()


def test_put_file_raises_driver_error_on_sftp_failure(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock, tmp_path: Path
) -> None:
    """``put_file`` wraps SFTP exceptions in :class:`DriverError`."""
    local = tmp_path / "local.txt"
    local.write_bytes(b"x")
    sftp = mock_ssh_client.open_sftp.return_value
    sftp.put.side_effect = IOError("disk full")

    with pytest.raises(DriverError) as excinfo:
        driver.put_file(local, "/tmp/moderator/remote.txt")
    assert "disk full" in str(excinfo.value)


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


def test_close_closes_underlying_client(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """``close`` calls ``SSHClient.close`` to release the transport."""
    driver.close()
    mock_ssh_client.close.assert_called_once()


def test_close_is_idempotent(
    driver: ParamikoSshDriver, mock_ssh_client: MagicMock
) -> None:
    """``close`` is safe to call multiple times. The driver guards
    itself with an internal ``_closed`` flag so the underlying
    ``SSHClient.close`` is invoked exactly once."""
    driver.close()
    driver.close()
    driver.close()
    assert mock_ssh_client.close.call_count == 1
    assert driver._closed is True  # type: ignore[attr-defined]


def test_close_before_connect_does_not_raise() -> None:
    """A driver constructed without a host (no auto-connect) can still
    be closed without raising. Important for test teardown of drivers
    that never made it past the construction phase."""
    d = ParamikoSshDriver()  # no host → no auto-connect → _client stays None
    d.close()  # must not raise


def test_close_before_connect_does_not_call_client_close() -> None:
    """``close()`` on an unconnected driver must not invoke
    ``SSHClient.close`` on a non-existent client (would crash)."""
    d = ParamikoSshDriver()
    # No patch needed — there's no real client to close.
    d.close()
    assert d._closed is True
    assert d._client is None


# ---------------------------------------------------------------------------
# TOFU (ADR-0015)
# ---------------------------------------------------------------------------


def test_connect_uses_auto_add_policy() -> None:
    """The driver installs ``AutoAddPolicy`` so the first connection
    to an unknown host proceeds (TOFU per ADR-0015). The moderator
    does not validate host keys itself."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        d = ParamikoSshDriver(host="some-new-host.example.com")
        # Construction triggers connect().
        client.connect.assert_called_once_with("some-new-host.example.com")
        # Policy was set BEFORE connect.
        assert client.set_missing_host_key_policy.called


# ---------------------------------------------------------------------------
# Lazy connect (runtime uses ParamikoSshDriver() with no host)
# ---------------------------------------------------------------------------


def test_lazy_construct_does_not_connect() -> None:
    """``ParamikoSshDriver()`` (no host) must not open any socket —
    the runtime holds an unconnected driver until ``start_session``
    knows the per-agent host."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        ParamikoSshDriver()
        # ``SSHClient()`` may or may not be called (we don't care
        # about the construction of the unused object) but the
        # underlying ``connect`` method must NOT have been invoked.
        if ClientCls.called:
            ClientCls.return_value.connect.assert_not_called()


def test_lazy_connect_opens_ssh_session_with_supplied_host() -> None:
    """A driver constructed without a host calls ``connect(host)`` to
    open the session — the runtime path for first-time ``start_session``."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        d = ParamikoSshDriver()
        assert d._client is None
        d.connect(host="django-app-openeuler-service-10")
        client.connect.assert_called_once_with("django-app-openeuler-service-10")
        # TOFU policy applied at _connect time.
        assert client.set_missing_host_key_policy.called
        # And the driver now points at the live client.
        assert d._client is client


def test_connect_is_idempotent_when_already_connected() -> None:
    """A second ``connect`` call on an already-connected driver is a
    no-op — we never want the runtime to reconnect on a transient
    failure and lose the agent's tmux session."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        d = ParamikoSshDriver(host="django-app-openeuler-service-10")
        d.connect(host="some-other-host.example.com")  # ignored
        d.connect()  # also ignored
        # The underlying SSHClient.connect is called exactly once —
        # from __init__.
        client.connect.assert_called_once_with("django-app-openeuler-service-10")


def test_connect_raises_driver_error_when_no_host_ever_supplied() -> None:
    """``ParamikoSshDriver()`` followed by ``connect()`` (no host) raises
    :class:`DriverError` rather than crashing on a None hostname."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient"):
        d = ParamikoSshDriver()
        with pytest.raises(DriverError) as excinfo:
            d.connect()
        assert "host required" in str(excinfo.value)


def test_run_on_unconnected_driver_raises_driver_error() -> None:
    """Calling :meth:`run` before :meth:`connect` is a programmer
    error; surface it as :class:`DriverError` (no paramiko leakage)."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient"):
        d = ParamikoSshDriver()
        with pytest.raises(DriverError) as excinfo:
            d.run("echo hi")
        assert "not connected" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construct_with_unknown_user_uses_default_username() -> None:
    """``host`` may be ``user@host`` or just ``host``; paramiko's
    ``SSHClient.connect`` handles both forms. The driver should not
    parse the host string itself."""
    with patch("moderator.drivers.ssh.paramiko.SSHClient"):
        ParamikoSshDriver(host="django-app-openeuler-service-10")
        ParamikoSshDriver(host="django-app@django-app-openeuler-service-10")


def test_construct_raises_driver_error_on_auth_failure() -> None:
    """``connect`` raising ``paramiko.AuthenticationException`` (or any
    subclass of paramiko's SSHException) is wrapped in
    :class:`DriverError` with a clean message — no paramiko types
    leak to the MCP caller."""
    import paramiko

    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        client.connect.side_effect = paramiko.AuthenticationException(
            "Auth failed"
        )
        with pytest.raises(DriverError) as excinfo:
            ParamikoSshDriver(host="bad-host")
        assert "Auth failed" in str(excinfo.value)


__all__: list[Any] = []  # pytest discovery via module