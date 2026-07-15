"""Process-wide runtime: drivers, paths, tunables.

Centralized so tests can swap implementations via
:func:`set_drivers` and so the production binary can be
configured via environment variables without scattering
``os.environ.get`` calls across the codebase.

Selection rules:

- ``MODERATOR_DRIVER=local``  → :class:`LocalExecutor` (default
  for the test suite; also useful for single-host development).
- ``MODERATOR_DRIVER=ssh``    → :class:`ParamikoSshDriver` +
  :class:`LibtmuxDriver`. Raises :class:`DriverMissing` if the
  optional dependencies aren't installed.

Swapping drivers at runtime is supported via :func:`set_drivers`
and is the recommended way for tests to inject a fixture.
"""

from __future__ import annotations

import os

from moderator.drivers import DriverMissing, SshDriver, TmuxDriver
from moderator.drivers.local import LocalExecutor


# Module-level singletons; tests reset via set_drivers.
_ssh: SshDriver | None = None
_tmux: TmuxDriver | None = None


def get_drivers() -> tuple[SshDriver, TmuxDriver]:
    """Return (ssh, tmux) for the current process.

    Constructs on first call. Subsequent calls return the same
    pair. Tests use :func:`set_drivers` to inject fixtures.
    """
    global _ssh, _tmux
    if _ssh is not None and _tmux is not None:
        return _ssh, _tmux
    choice = os.environ.get("MODERATOR_DRIVER", "local").lower()
    if choice == "local":
        execu = LocalExecutor()
        _ssh, _tmux = execu, execu
    elif choice in ("ssh", "tmux"):
        # Real drivers. Defer the import so the test env doesn't
        # have to install paramiko/libtmux.
        from moderator.drivers.ssh import ParamikoSshDriver  # type: ignore[import-not-found]  # noqa
        from moderator.drivers.tmux_lib import LibtmuxDriver  # type: ignore[import-not-found]  # noqa
        _ssh, _tmux = ParamikoSshDriver(), LibtmuxDriver()
    else:
        raise DriverMissing(f"unknown MODERATOR_DRIVER: {choice!r}")
    return _ssh, _tmux


def set_drivers(ssh: SshDriver, tmux: TmuxDriver) -> None:
    """Inject a driver pair. Test-only API."""
    global _ssh, _tmux
    _ssh, _tmux = ssh, tmux


def reset_drivers() -> None:
    """Drop cached drivers. Test-only API."""
    global _ssh, _tmux
    _ssh, _tmux = None, None


__all__ = [
    "DriverMissing",
    "SshDriver",
    "TmuxDriver",
    "get_drivers",
    "reset_drivers",
    "set_drivers",
]
