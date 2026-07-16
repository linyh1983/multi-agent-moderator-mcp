"""``ParamikoSshDriver`` — real SSH driver backed by ``paramiko``.

Closes the gap surfaced during manual integration testing on
2026-07-15: ticket 02 advertised "real SSH/tmux driver" but only
delivered :class:`LocalExecutor`. Until this class landed,
``MODERATOR_DRIVER=ssh`` raised
``ParamikoSshDriver is not yet implemented``.

This module is intentionally minimal. It implements exactly the
:class:`SshDriver` Protocol surface — :meth:`put_file`, :meth:`run`,
:meth:`close` — plus an explicit :meth:`connect` so the runtime
can defer SSH connection until it knows the per-agent host
(host comes from the MCP ``start_session`` call, not from process
startup). Higher-level tmux semantics live in
:mod:`moderator.drivers.tmux_remote`, which delegates to this
driver's :meth:`run`.

Trust model (ADR-0015):

- First connection to an unknown host proceeds (``AutoAddPolicy``) —
  the moderator trusts TOFU for v1.
- Future work may add a fingerprint allowlist. Until then, the user
  is responsible for verifying the host key on first contact.

Auth:

- Key-based auth via paramiko's defaults (``~/.ssh/id_rsa``,
  ``~/.ssh/id_ed25519``, SSH agent).
- Never reads or stores passwords.
- Username: extracted from ``host`` if formatted as ``user@host``;
  otherwise paramiko uses the current local username.

Connection lifecycle:

- Construct with ``host=...`` to connect eagerly. Construct with
  no host to defer; call :meth:`connect` before :meth:`run` or
  :meth:`put_file`.
- One :class:`paramiko.SSHClient` per driver instance, reused
  across all :meth:`run` calls.
- :meth:`connect` is idempotent — a second call is a no-op.
- :meth:`close` is idempotent and safe to call on an unconnected
  driver (test teardown convenience).
"""

from __future__ import annotations

import logging
from pathlib import Path

import paramiko

from moderator.drivers import DriverError


_log = logging.getLogger(__name__)


class ParamikoSshDriver:
    """Real :class:`SshDriver` implementation backed by paramiko.

    Two construction modes:

    - ``ParamikoSshDriver(host="...")`` — eager: opens the SSH
      connection immediately so auth failures surface at construction
      time. This matches :class:`LocalExecutor`'s "fail fast" semantics
      when the host is known up front.
    - ``ParamikoSshDriver()`` — lazy: returns an unconnected driver.
      Call :meth:`connect` with the actual host before the first
      :meth:`run` or :meth:`put_file` call. The runtime uses this form
      because the per-agent host is only known at MCP ``start_session``
      time, not at process startup.

    The class is deliberately not slotted — paramiko's SSHClient
    carries its own state and we want plain attribute access for
    test injection (see the ``_client`` attribute and
    ``tests/drivers/test_ssh.py``).
    """

    def __init__(self, host: str | None = None) -> None:
        self._host = host
        # Tests inject a mock client via ``d._client = ...`` before
        # any other method is called. We track whether the client has
        # been closed so :meth:`close` can be safely idempotent.
        self._client: paramiko.SSHClient | None = None
        self._closed = False
        if host is not None:
            self._connect()

    # ------------------------------------------------------------------
    # Connection setup
    # ------------------------------------------------------------------

    def connect(self, host: str | None = None) -> None:
        """Open the SSH connection. Idempotent — a second call is a
        no-op once :attr:`_client` is set.

        ``host`` is required unless one was supplied at construction.
        If both are None, the driver stays unconnected and any later
        :meth:`run` / :meth:`put_file` call raises :class:`DriverError`.
        """
        if self._client is not None:
            return
        if host is not None:
            self._host = host
        if self._host is None:
            raise DriverError(
                "ParamikoSshDriver.connect: host required "
                "(neither constructor nor connect() received one)"
            )
        self._connect()

    def _connect(self) -> None:
        """Open the SSH connection. Called from :meth:`__init__` (when
        host was supplied) and from :meth:`connect` (lazy mode)."""
        assert self._host is not None  # callers gate on this
        client = paramiko.SSHClient()
        # TOFU per ADR-0015: first connection proceeds; future
        # iterations may consult a fingerprint allowlist.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self._host)
        except paramiko.AuthenticationException as exc:
            raise DriverError(
                f"ParamikoSshDriver({self._host!r}): auth failed: {exc}"
            ) from exc
        except paramiko.SSHException as exc:
            raise DriverError(
                f"ParamikoSshDriver({self._host!r}): SSH error: {exc}"
            ) from exc
        except OSError as exc:
            raise DriverError(
                f"ParamikoSshDriver({self._host!r}): connection error: {exc}"
            ) from exc
        self._client = client

    # ------------------------------------------------------------------
    # SshDriver protocol
    # ------------------------------------------------------------------

    def put_file(self, local: Path, remote: str) -> None:
        """Copy ``local`` to ``remote`` on the host. Creates parent
        directories on demand (idempotent: existing dirs are left
        alone)."""
        client = self._require_client()
        sftp = client.open_sftp()
        try:
            self._ensure_remote_dir(sftp, remote)
            # paramiko's ``sftp.put`` accepts a local path string and
            # opens / reads it itself; this is simpler than handing it
            # a file-like object and avoids any lifecycle question about
            # the open handle.
            sftp.put(str(local), remote)
        except OSError as exc:
            raise DriverError(
                f"ParamikoSshDriver.put_file({local} -> {remote}): {exc}"
            ) from exc
        finally:
            sftp.close()

    def run(self, command: str) -> tuple[int, str, str]:
        """Run ``command`` on the host. Returns ``(rc, stdout, stderr)``.

        A non-zero ``rc`` is a normal return value — the caller decides
        what it means. Only transport-level failures (channel closed,
        SFTP error, socket reset) raise :class:`DriverError`."""
        client = self._require_client()
        try:
            _stdin, stdout_file, stderr_file = client.exec_command(command)
        except paramiko.SSHException as exc:
            raise DriverError(
                f"ParamikoSshDriver.run({command!r}): {exc}"
            ) from exc
        except OSError as exc:
            raise DriverError(
                f"ParamikoSshDriver.run({command!r}): {exc}"
            ) from exc
        # Read fully before fetching exit status — paramiko's docs warn
        # that ``recv_exit_status`` may block until the channel is
        # consumed if the command produces a lot of output.
        out_bytes = stdout_file.read()
        err_bytes = stderr_file.read()
        rc = stdout_file.channel.recv_exit_status()
        return (
            rc,
            out_bytes.decode("utf-8", errors="replace"),
            err_bytes.decode("utf-8", errors="replace"),
        )

    def close(self) -> None:
        """Release the underlying transport. Idempotent."""
        if self._closed:
            return
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:  # noqa: BLE001
                _log.debug("ParamikoSshDriver.close: %s", exc)
            self._client = None
        self._closed = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_client(self) -> paramiko.SSHClient:
        """Return the live SSHClient or raise :class:`DriverError` if
        the driver has been closed."""
        if self._client is None:
            raise DriverError(
                f"ParamikoSshDriver({self._host!r}): not connected "
                f"(called after close)"
            )
        return self._client

    @staticmethod
    def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote: str) -> None:
        """Create parent directories of ``remote`` if absent.

        Walks the path from deepest to shallowest, calling
        ``sftp.mkdir`` until one succeeds or already exists. Tolerates
        races / already-exists errors per the spec.
        """
        parts = remote.split("/")
        # Drop the trailing filename — we only mkdir on parent dirs.
        parents = parts[:-1]
        # Reconstruct path candidates from the deepest upward.
        candidates: list[str] = []
        for i in range(len(parents), 0, -1):
            joined = "/".join(parents[:i])
            # ``split("/")`` on an absolute path produces an empty
            # leading element, so ``"/".join(['', 'tmp', ...])``
            # already starts with '/'. Only prepend '/' for relative
            # paths (when there's no leading empty element).
            if joined and not joined.startswith("/"):
                joined = "/" + joined
            if joined:
                candidates.append(joined)
        for path in candidates:
            try:
                sftp.stat(path)
            except OSError:
                # Doesn't exist — create it.
                try:
                    sftp.mkdir(path)
                except OSError:
                    # Race or already-exists (some servers raise on
                    # mkdir even when stat previously failed). Tolerate.
                    pass


__all__ = ["ParamikoSshDriver"]