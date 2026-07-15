"""Tests for the driver protocols + LocalExecutor.

Covers:

- ``LocalExecutor`` satisfies both ``SshDriver`` and ``TmuxDriver``
  protocols (runtime_checkable ``isinstance``).
- ``create_session`` records the original command (test helper).
- ``send_keys`` round-trips through the echo agent.
- ``capture_pane`` returns the recent stdout tail.
- ``is_alive`` is True while the echo agent runs, False after
  ``kill_session``.
- ``put_file`` writes to a simulated remote root and refuses
  ``..`` escape via ``/``.
- Workdir boundary: ``workdir`` is the simulated ``/`` of the
  fake host.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from moderator.drivers import (
    DriverError,
    SshDriver,
    TmuxDriver,
    driver_pair,
)
from moderator.drivers.local import LocalExecutor


@pytest.fixture
def executor(tmp_path: Path) -> Generator[LocalExecutor, None, None]:
    workdir = tmp_path / "remote"
    workdir.mkdir(parents=True, exist_ok=True)
    exe = LocalExecutor(workdir=workdir)
    try:
        yield exe
    finally:
        # Subprocess-backed sessions must be terminated explicitly;
        # LocalExecutor.__del__ is best-effort and won't run promptly
        # enough to keep pytest from hanging on the echo agent's stdin.
        try:
            exe.close()
        except Exception:
            pass


def test_local_executor_satisfies_both_protocols(executor: LocalExecutor) -> None:
    assert isinstance(executor, SshDriver)
    assert isinstance(executor, TmuxDriver)


def test_create_session_records_command_via_helper(executor: LocalExecutor) -> None:
    """The driver stores the command in shell-quoted form so a
    later caller can re-execute it; the test helper returns that
    same quoted form."""
    import shlex

    executor.create_session("mod-x", "echo hello")
    assert executor.get_command("mod-x") == shlex.quote("echo hello")


def test_is_alive_true_while_running(executor: LocalExecutor) -> None:
    executor.create_session("mod-x", "echo hi")
    assert executor.is_alive("mod-x") is True


def test_is_alive_false_after_kill(executor: LocalExecutor) -> None:
    executor.create_session("mod-x", "echo hi")
    executor.kill_session("mod-x")
    assert executor.is_alive("mod-x") is False


def test_kill_session_is_idempotent(executor: LocalExecutor) -> None:
    executor.kill_session("never-existed")
    executor.create_session("mod-x", "echo hi")
    executor.kill_session("mod-x")
    executor.kill_session("mod-x")  # second call: no-op


def test_send_keys_round_trips_through_echo_agent(executor: LocalExecutor) -> None:
    executor.create_session("mod-x", "ignored")
    executor.send_keys("mod-x", "hello world")
    # The daemon reader drains the pipe asynchronously; wait for at
    # least the cmd prefix + the echo to land in the buffer.
    assert executor.wait_for_buffer_at_least("mod-x", 20)
    pane = executor.capture_pane("mod-x", 100)
    assert "hello world" in pane


def test_capture_pane_lines_param_limits_output(executor: LocalExecutor) -> None:
    executor.create_session("mod-x", "ignored")
    for i in range(20):
        executor.send_keys("mod-x", f"line {i}")
    # Wait for the last "line 19" to land in the buffer.
    assert executor.wait_for_buffer_at_least("mod-x", 100)
    pane = executor.capture_pane("mod-x", 5)
    # We asked for the last 5 lines; should not include line 0.
    assert "line 0" not in pane
    assert "line 19" in pane


def test_put_file_absolute_posix_path_lands_under_workdir(
    executor: LocalExecutor, tmp_path: Path
) -> None:
    local = tmp_path / "src.txt"
    local.write_text("payload", encoding="utf-8")
    executor.put_file(local, "/tmp/role.txt")
    target = executor.workdir / "tmp" / "role.txt"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "payload"


def test_put_file_relative_path_lands_under_workdir(
    executor: LocalExecutor, tmp_path: Path
) -> None:
    local = tmp_path / "src.txt"
    local.write_text("payload", encoding="utf-8")
    executor.put_file(local, "subdir/role.txt")
    target = executor.workdir / "subdir" / "role.txt"
    assert target.exists()


def test_put_file_refuses_parent_escape(executor: LocalExecutor) -> None:
    """Path-traversal guard: ``..`` segments must not escape workdir."""
    local = Path(__file__)  # any existing file
    with pytest.raises(DriverError):
        executor.put_file(local, "/../escape.txt")


def test_run_returns_rc_stdout_stderr(executor: LocalExecutor) -> None:
    rc, out, err = executor.run("python -c \"print('hi'); import sys; sys.stderr.write('bye')\"")
    assert rc == 0
    assert "hi" in out
    assert "bye" in err


def test_run_propagates_nonzero_rc(executor: LocalExecutor) -> None:
    rc, _, _ = executor.run("python -c \"import sys; sys.exit(7)\"")
    assert rc == 7


def test_driver_pair_closes_ssh_on_exit(tmp_path: Path) -> None:
    """driver_pair must close the SSH side on context exit."""
    workdir = tmp_path / "remote"
    workdir.mkdir(parents=True, exist_ok=True)
    execu = LocalExecutor(workdir=workdir)
    with driver_pair(execu, execu) as (ssh, tmux):
        assert isinstance(ssh, SshDriver)
        assert isinstance(tmux, TmuxDriver)
    assert execu._closed is True  # type: ignore[attr-defined]


def test_send_keys_to_dead_session_raises(executor: LocalExecutor) -> None:
    with pytest.raises(DriverError):
        executor.send_keys("never-existed", "hi")


def test_double_create_session_on_alive_raises(executor: LocalExecutor) -> None:
    executor.create_session("mod-x", "echo hi")
    with pytest.raises(DriverError):
        executor.create_session("mod-x", "echo again")
