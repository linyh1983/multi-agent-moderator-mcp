"""In-process ``LocalExecutor`` — subprocess-backed driver pair.

Useful for tests and for single-host development where the user
wants to run the moderator without an actual remote machine. Each
"tmux session" is a child subprocess; bytes written to the
subprocess's stdin are visible to its stdout.

The executor is intentionally simple: it does not pretend to
model tmux sessions, panes, or buffers. It does what the
moderator's worker needs — ``send_keys`` becomes ``stdin.write``,
``capture_pane`` becomes ``stdout.read`` (drained into a per-session
buffer the worker advances through), and ``is_alive`` is
``proc.poll() is None``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from moderator.drivers import DriverError, SshDriver, TmuxDriver


class _Session:
    """In-process state for one fake tmux session."""

    def __init__(self, name: str, proc: subprocess.Popen[bytes]) -> None:
        self.name = name
        self.proc = proc
        # Bounded buffer of stdout bytes the worker hasn't consumed yet.
        # A real tmux capture-pane is non-destructive; we mimic that
        # by retaining all bytes and tracking the worker's offset.
        self.stdout_buf = bytearray()
        self._lock = threading.Lock()
        # A daemon thread continuously drains the subprocess stdout
        # into ``stdout_buf``. ``capture_pane`` then reads from the
        # buffer (non-blocking) rather than calling ``read1`` on the
        # pipe, which would block until the next byte arrives.
        self._reader = threading.Thread(
            target=self._drain, name=f"moderator-local-exec:{name}", daemon=True
        )
        self._reader.start()

    def _drain(self) -> None:
        """Continuously read stdout until EOF. Runs on a daemon thread."""
        assert self.proc.stdout is not None
        try:
            while True:
                # ``read1`` is a method on ``BufferedIOBase``; the
                # type stubs for ``IO[Any]`` don't surface it. The
                # runtime call works because subprocess pipes are
                # buffered streams.
                chunk = self.proc.stdout.read1(4096)  # type: ignore[attr-defined]
                if not chunk:
                    return
                with self._lock:
                    self.stdout_buf.extend(chunk)
        except (ValueError, OSError):
            # Pipe closed or process gone; we're done.
            return

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.stdout_buf.extend(chunk)

    def read_from(self, offset: int) -> tuple[int, bytes]:
        """Return (new_offset, new_bytes) since ``offset``."""
        with self._lock:
            data = bytes(self.stdout_buf[offset:])
        return len(self.stdout_buf), data

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class LocalExecutor:
    """Implements both :class:`SshDriver` and :class:`TmuxDriver`.

    ``run`` invokes a subprocess synchronously. ``put_file`` writes
    the local file to a host-local staging directory. The two are
    decoupled — there's no real SSH; this exists for the worker
    and tool logic that doesn't care about transport.
    """

    def __init__(self, *, workdir: Path | None = None) -> None:
        self.workdir = workdir or Path.cwd() / ".local-exec"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    # ----- SshDriver -----

    def connect(self, host: str | None = None) -> None:
        """No-op for the in-process simulator. Mirrors
        :meth:`ParamikoSshDriver.connect` so call sites that wire
        up a driver pair before the first network call don't have
        to branch on driver kind. ticket 12 / bug B1."""

    def put_file(self, local: Path, remote: str) -> None:
        # ``workdir`` is the simulated remote root (think ``/`` on the
        # agent host). Absolute POSIX paths are joined as if workdir
        # were the filesystem root; relative paths join under it.
        # Refuse any escape via ``..`` segments regardless of form.
        rel = remote.lstrip("/") if remote.startswith("/") else remote
        target = (self.workdir / rel).resolve()
        if not str(target).startswith(str(self.workdir.resolve())):
            raise DriverError(f"refusing to write outside workdir: {remote}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local.read_bytes())

    def run(self, command: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(self.workdir),
            capture_output=True,
            check=False,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def close(self) -> None:
        self._closed = True
        for s in list(self._sessions.values()):
            try:
                s.proc.terminate()
            except Exception:
                pass

    # ----- TmuxDriver -----

    def is_alive(self, session: str) -> bool:
        s = self._sessions.get(session)
        return bool(s and s.alive)

    def create_session(self, session: str, command: str) -> None:
        if session in self._sessions and self._sessions[session].alive:
            raise DriverError(f"session already exists: {session!r}")
        # Spawn a long-running child that just echoes stdin to stdout.
        # We don't actually run ``command`` — the LocalExecutor is
        # for testing the moderator's plumbing, not the agent itself.
        # We do, however, record the command so tests can assert on it.
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _ECHO_AGENT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workdir),
        )
        self._sessions[session] = _Session(session, proc)
        # Surface the original command via a special stdout prefix.
        self._sessions[session].feed(
            f"<<cmd: {shlex.quote(command)}>>\n".encode("utf-8")
        )

    def send_keys(self, session: str, text: str) -> None:
        s = self._sessions.get(session)
        if not s or not s.alive:
            raise DriverError(f"no such session: {session!r}")
        assert s.proc.stdin is not None
        # Echo agent turns "\n" into a line. "Enter" is "\n".
        s.proc.stdin.write((text + "\n").encode("utf-8"))
        s.proc.stdin.flush()

    def paste_buffer(self, session: str, text: str) -> None:
        # No real buffer in the local executor; send_keys is enough.
        self.send_keys(session, text)

    def capture_pane(self, session: str, lines: int) -> str:
        s = self._sessions.get(session)
        if not s or not s.alive:
            raise DriverError(f"no such session: {session!r}")
        # The background reader has already drained the pipe into
        # ``s.stdout_buf``; we just need to read the last N lines
        # from it. This is non-blocking — it never waits for new
        # bytes from the agent process.
        text = bytes(s.stdout_buf).decode("utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-lines:])
        return tail

    def read_new_bytes(
        self, session: str, since_offset: int
    ) -> tuple[int, bytes]:
        s = self._sessions.get(session)
        if s is None:
            return since_offset, b""
        return s.read_from(since_offset)

    def kill_session(self, session: str) -> None:
        s = self._sessions.pop(session, None)
        if s is None:
            return
        try:
            s.proc.terminate()
            try:
                s.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                s.proc.kill()
                s.proc.wait(timeout=1.0)
        except Exception:
            pass

    # ----- helpers exposed for tests / the worker -----

    def read_from(self, session: str, offset: int) -> tuple[int, bytes]:
        s = self._sessions.get(session)
        if s is None:
            return offset, b""
        return s.read_from(offset)

    def get_command(self, session: str) -> str | None:
        """Return the command used to create ``session`` (test helper)."""
        s = self._sessions.get(session)
        if s is None:
            return None
        text = bytes(s.stdout_buf).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("<<cmd: "):
                return line[7:-2]
        return None

    def wait_for_buffer_at_least(
        self, session: str, min_bytes: int, *, timeout: float = 2.0
    ) -> bool:
        """Test helper: block until ``session``'s stdout buffer has
        grown to at least ``min_bytes`` bytes, or ``timeout`` elapses.

        Returns True on success, False on timeout. The echo agent
        processes one line at a time; this is the test's way to wait
        for the daemon reader thread to catch up after ``send_keys``.
        """
        s = self._sessions.get(session)
        if s is None:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with s._lock:  # type: ignore[attr-defined]
                size = len(s.stdout_buf)
            if size >= min_bytes:
                return True
            time.sleep(0.01)
        return False

    # Make it also satisfy the SshDriver/TmuxDriver protocols
    # without inheriting (Protocols can't be inherited as classes
    # in this form). runtime_checkable on the protocols means
    # ``isinstance(x, SshDriver)`` works at call sites anyway.
    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass


# The echo agent is intentionally a separate process so that
# subprocess lifecycle (poll, kill, wait) is exercised the same
# way the real SSH/tmux driver would exercise it.
_ECHO_AGENT = """
import sys
for line in sys.stdin:
    sys.stdout.write(line)
    sys.stdout.flush()
"""


# Re-export so ``from moderator.drivers.local import LocalExecutor``
# reads naturally.
__all__ = ["LocalExecutor"]


# Marker so tests can assert subprocess env if needed.
os.environ.setdefault("MODERATOR_LOCAL_EXEC", "1")