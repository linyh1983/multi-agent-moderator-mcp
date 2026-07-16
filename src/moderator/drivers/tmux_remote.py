"""``RemoteTmuxDriver`` — tmux operations over SSH.

The driver satisfies the :class:`TmuxDriver` Protocol by shelling out
to the remote host's ``tmux(1)`` binary via the supplied
:class:`SshDriver`. Every method becomes one or more ``ssh.run``
calls; the driver itself owns no per-session state on the local
side beyond a small byte buffer used to satisfy
:meth:`read_new_bytes`'s incremental-offset contract.

Implementation notes:

- ``read_new_bytes`` captures the current pane each call and stores
  it in a per-session buffer; subsequent calls return bytes past
  the worker's last offset. If the pane scrolls past the buffered
  content (which happens for long-running sessions), the buffer is
  reset and the worker sees a fresh stream. The worker's marker
  dedup (ADR-0013 §5.4) absorbs the re-feed cost.
- ``paste_buffer`` uses SFTP to stage the buffer text on the remote
  host (``/tmp/moderator-buffer-<session>-<ts>.txt``), then runs
  ``tmux load-buffer`` + ``tmux paste-buffer`` against the file.
  This avoids needing stdin support on :class:`SshDriver`.
- ``kill_session`` tolerates a non-zero rc — the session may already
  be dead (e.g. on user cleanup) and we don't want to surface that
  as an error.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from moderator.drivers import DriverError, SshDriver, TmuxDriver


if TYPE_CHECKING:
    pass


_log = logging.getLogger(__name__)


# Default number of pane lines captured by ``capture_pane`` /
# ``read_new_bytes``. Large enough to cover a typical agent turn;
# not so large that tmux's own buffer becomes the bottleneck.
_DEFAULT_PANE_LINES = 10_000


class RemoteTmuxDriver(TmuxDriver):  # type: ignore[misc]  # Protocol-as-base, see note below
    """Tmux driver that delegates every call to a remote ``tmux(1)``
    over an :class:`SshDriver` connection.

    The Protocol-as-base (``TmuxDriver``) is a ``runtime_checkable``
    Protocol and isn't really meant to be inherited as a class. We
    do it here only so a quick ``RemoteTmuxDriver(...)`` call
    surfaces unimplemented methods at instantiation via MRO. The
    actual runtime contract is ``isinstance(d, TmuxDriver)``.
    """

    def __init__(self, exec_via: SshDriver) -> None:
        self._ssh = exec_via
        # Per-session byte buffers for ``read_new_bytes``. Keyed by
        # tmux session name.
        self._buffers: dict[str, bytearray] = {}
        # Per-session last-seen pane content, used to short-circuit
        # ``read_new_bytes`` when the pane hasn't changed.
        self._last_data: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # TmuxDriver protocol
    # ------------------------------------------------------------------

    def is_alive(self, session: str) -> bool:
        """True iff ``tmux has-session`` exits 0 on the host."""
        rc, _, _ = self._ssh.run(f"tmux has-session -t {self._q(session)}")
        return rc == 0

    def create_session(self, session: str, command: str) -> None:
        """Start a detached tmux session running ``command``."""
        rc, _, err = self._ssh.run(
            f"tmux new-session -d -s {self._q(session)} {self._q(command)}"
        )
        if rc != 0:
            raise DriverError(
                f"RemoteTmuxDriver.create_session({session!r}): "
                f"tmux new-session exited {rc}: {err.strip()}"
            )

    def send_keys(self, session: str, text: str) -> None:
        """Send ``text`` followed by Enter to the session."""
        rc, _, err = self._ssh.run(
            f"tmux send-keys -t {self._q(session)} {self._q(text)}"
        )
        if rc != 0:
            raise DriverError(
                f"RemoteTmuxDriver.send_keys({session!r}): "
                f"tmux send-keys exited {rc}: {err.strip()}"
            )

    def paste_buffer(self, session: str, text: str) -> None:
        """Stage ``text`` on the remote host via SFTP, then load it
        into the tmux paste buffer and paste it into ``session``."""
        # 1. Stage locally so paramiko's SFTP can read it.
        fd, tmp_name = tempfile.mkstemp(
            prefix="moderator-buffer-", suffix=".txt"
        )
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
            local_tmp = Path(tmp_name)
        except BaseException:
            # mkstemp leaves the file behind on failure; clean up.
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass
            raise
        # 2. Choose a remote path the user is unlikely to clobber.
        # ``time.time()`` is unique enough for v1; collisions just
        # produce a confusing tmux error, not data corruption.
        remote_tmp = (
            f"/tmp/moderator-buffer-{self._q_safe(session)}-"
            f"{int(time.time() * 1000)}.txt"
        )
        try:
            self._ssh.put_file(local_tmp, remote_tmp)
            # 3. Load the file into the tmux paste buffer, paste, and
            # remove the staging file. The ``&&`` chain stops on
            # first failure; the rc check surfaces it as
            # :class:`DriverError`.
            rc, _, err = self._ssh.run(
                f"tmux load-buffer {self._q(remote_tmp)} && "
                f"tmux paste-buffer -t {self._q(session)} && "
                f"rm -f {self._q(remote_tmp)}"
            )
            if rc != 0:
                raise DriverError(
                    f"RemoteTmuxDriver.paste_buffer({session!r}): "
                    f"load/paste exited {rc}: {err.strip()}"
                )
        finally:
            try:
                local_tmp.unlink()
            except OSError:
                pass

    def capture_pane(self, session: str, lines: int) -> str:
        """Return up to ``lines`` of recent pane content."""
        rc, out, err = self._ssh.run(
            f"tmux capture-pane -t {self._q(session)} -p -S -{lines}"
        )
        if rc != 0:
            raise DriverError(
                f"RemoteTmuxDriver.capture_pane({session!r}): "
                f"tmux capture-pane exited {rc}: {err.strip()}"
            )
        return out

    def read_new_bytes(
        self, session: str, since_offset: int
    ) -> tuple[int, bytes]:
        """Return ``(new_offset, new_bytes)`` since ``since_offset``.

        Each call captures the current pane content and stores it in
        a per-session buffer. To find what's "new" we compare the
        current capture to the previous one and compute the longest
        common prefix — only bytes past the prefix are appended. This
        handles the typical agent-typing case (line N+1 appended)
        without re-feeding old content to the parser.

        When the pane content is unchanged from the previous call,
        returns ``(since_offset, b"")`` so the worker doesn't re-parse
        identical bytes.

        If the pane scrolled past the buffered content, the buffer is
        reset and the worker sees a fresh stream — the dedup FIFO
        (ADR-0013 §5.4) absorbs the re-feed.
        """
        pane = self.capture_pane(session, _DEFAULT_PANE_LINES)
        data = pane.encode("utf-8")

        # Quick dedup: if the pane hasn't changed, don't extend.
        last = self._last_data.get(session)
        if last == data:
            return since_offset, b""

        # Common-prefix dedup: find how much of the previous capture
        # is also at the start of the new one. Only the suffix past
        # the common part is genuinely new content.
        common = 0
        if last is not None:
            min_len = min(len(last), len(data))
            while common < min_len and last[common] == data[common]:
                common += 1
        new_suffix = data[common:]

        self._last_data[session] = data

        buf = self._buffers.setdefault(session, bytearray())
        # Pane scrolled past what we had buffered — start fresh.
        if len(buf) < since_offset:
            buf.clear()
            since_offset = 0
        buf.extend(new_suffix)
        return len(buf), bytes(buf[since_offset:])

    def kill_session(self, session: str) -> None:
        """Tear the session down. Tolerates non-zero rc (already dead)."""
        rc, _, err = self._ssh.run(
            f"tmux kill-session -t {self._q(session)}"
        )
        if rc != 0:
            # Already-dead is the normal case during cleanup — log at
            # debug, don't raise. Other failures (e.g. permissions)
            # surface via the log; the worker treats this as a no-op.
            _log.debug(
                "RemoteTmuxDriver.kill_session(%r): rc=%d stderr=%s",
                session, rc, err.strip(),
            )
        # Drop any cached buffer + last-data for this session so a
        # future session with the same name starts clean.
        self._buffers.pop(session, None)
        self._last_data.pop(session, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _q(s: str) -> str:
        """Shell-quote a string for inclusion in a remote command.

        Uses :func:`shlex.quote` so session names, commands, and
        text are safe regardless of contents. ``shlex.quote`` does
        not handle embedded newlines for ``bash -c`` invocations;
        for those cases the caller should use a heredoc (we don't
        currently need one because all tmux subcommands here are
        single-line).
        """
        import shlex

        return shlex.quote(s)

    @staticmethod
    def _q_safe(s: str) -> str:
        """Shell-quote without leading slashes — for embedding into a
        filename component (``/tmp/foo-<q_safe>-bar``).

        ``shlex.quote`` would prefix single quotes (``'/tmp/foo'``),
        which is fine for command args but produces ugly filenames.
        For filenames we want the contents only, with any path-
        separators and shell metacharacters escaped.
        """
        import re

        # Replace anything that's not alphanumeric / dash / underscore /
        # dot with an underscore. Good enough for filenames.
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


__all__ = ["RemoteTmuxDriver"]