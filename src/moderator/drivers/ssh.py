"""Stub for the real SSH driver (paramiko-based).

This module is intentionally a TODO marker. The
:class:`ParamikoSshDriver` will be implemented when a real SSH
test host is available; for now any import of this module
raises :class:`DriverMissing` so the failure is loud and
early.

Selection (per :mod:`moderator.runtime`): set
``MODERATOR_DRIVER=ssh`` to attempt to use it. Without
paramiko installed, the import in :mod:`moderator.runtime`
will fail with the message below.
"""

from __future__ import annotations

from pathlib import Path

from moderator.drivers import DriverMissing, SshDriver


class ParamikoSshDriver(SshDriver):
    """Real SSH driver — NOT YET IMPLEMENTED.

    v1 acceptance (ticket 02) does not require a real SSH test
    host. The local executor covers the moderator's plumbing
    end-to-end. This stub is here so the :mod:`moderator.runtime`
    factory can import a name; it will fail loudly if a caller
    actually tries to use it without paramiko installed and a
    test host reachable.
    """

    def __init__(self) -> None:
        raise DriverMissing(
            "ParamikoSshDriver is not yet implemented; ticket 02 "
            "covers the local executor. Set MODERATOR_DRIVER=local "
            "for development, or install paramiko and implement "
            "this driver before production deployment."
        )

    def put_file(self, local: Path, remote: str) -> None:
        raise DriverMissing("ParamikoSshDriver.put_file: TODO")

    def run(self, command: str) -> tuple[int, str, str]:
        raise DriverMissing("ParamikoSshDriver.run: TODO")

    def close(self) -> None:
        return None


__all__ = ["ParamikoSshDriver"]