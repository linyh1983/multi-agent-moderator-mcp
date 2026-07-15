"""Remote Agent drivers.

Per ADR-0003, v1 uses SSH + tmux. The real drivers are behind
optional dependencies (``drivers[ssh]``); the ``LocalExecutor``
runs the same protocol against subprocesses in-process so the
worker + tool logic can be tested without an SSH test host.

Public surface:

- :class:`SshDriver` / :class:`TmuxDriver`: protocols every
  implementation must satisfy.
- :class:`LocalExecutor`: in-process subprocess-backed
  implementation. Always available.
- :class:`ParamikoSshDriver` / :class:`LibtmuxDriver`: real
  production drivers. Available only with the relevant optional
  dep installed. Construction raises :class:`DriverMissing`
  if the dep is absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable


class DriverError(Exception):
    """Base for any driver-layer failure surfaced to the MCP caller."""


class DriverMissing(DriverError):
    """The optional dependency for a real driver is not installed."""


@runtime_checkable
class SshDriver(Protocol):
    """Minimal SSH surface needed by the moderator.

    Implementations are responsible for TOFU fingerprint handling
    (ADR-0015); the moderator does not validate host keys itself.
    """

    def put_file(self, local: Path, remote: str) -> None:
        """Copy ``local`` to ``remote`` on the host. May create dirs."""
        ...

    def run(self, command: str) -> tuple[int, str, str]:
        """Run ``command`` on the host. Returns (rc, stdout, stderr)."""
        ...

    def close(self) -> None:
        """Release the connection. Idempotent."""
        ...


@runtime_checkable
class TmuxDriver(Protocol):
    """Minimal tmux surface needed by the moderator.

    All operations take a session name; the moderator never asks
    the driver to enumerate sessions. If a session is missing, the
    method either returns a sentinel (``False``/empty) or raises
    :class:`DriverError`.
    """

    def is_alive(self, session: str) -> bool:
        """``True`` iff the tmux session exists on the host."""
        ...

    def create_session(self, session: str, command: str) -> None:
        """Start a detached session running ``command``."""
        ...

    def send_keys(self, session: str, text: str) -> None:
        """Send a single keystroke batch (terminated with Enter)."""
        ...

    def paste_buffer(self, session: str, text: str) -> None:
        """Load ``text`` as a tmux buffer and paste it to the session."""
        ...

    def capture_pane(self, session: str, lines: int) -> str:
        """Return up to ``lines`` of recent pane content."""
        ...

    def read_new_bytes(
        self, session: str, since_offset: int
    ) -> tuple[int, bytes]:
        """Return ``(new_offset, new_bytes)`` — every byte the
        session has produced on its stdout since ``since_offset``.

        ``new_offset`` is the total byte count the driver has now
        seen, suitable for passing back on the next call. The byte
        stream is non-destructive; nothing is consumed.

        The moderator's worker uses this to drive the per-agent
        parser so it can re-poll without re-parsing bytes it has
        already seen. The real tmux driver is expected to buffer
        the per-session stdout stream itself (the moderator cannot
        rely on tmux's capture-pane to give exact offsets).
        """
        ...

    def kill_session(self, session: str) -> None:
        """Tear the session down. No-op if already gone."""
        ...


@contextmanager
def driver_pair(
    ssh: SshDriver, tmux: TmuxDriver
) -> Iterator[tuple[SshDriver, TmuxDriver]]:
    """Context manager that closes the SSH driver on exit.

    tmux sessions are owned by the host, not the driver — they
    outlive the driver. Closing SSH releases the connection.
    """
    try:
        yield ssh, tmux
    finally:
        try:
            ssh.close()
        except DriverError:
            pass


__all__ = [
    "DriverError",
    "DriverMissing",
    "SshDriver",
    "TmuxDriver",
    "driver_pair",
]
