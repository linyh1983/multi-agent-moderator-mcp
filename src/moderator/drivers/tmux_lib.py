"""Stub for the real tmux driver (libtmux-based).

Same TODO status as :mod:`moderator.drivers.ssh` —
:class:`LibtmuxDriver` will be implemented when a real SSH test
host with tmux installed is available. For now the constructor
raises :class:`DriverMissing`.
"""

from __future__ import annotations

from moderator.drivers import DriverMissing, TmuxDriver


class LibtmuxDriver(TmuxDriver):
    """Real tmux driver — NOT YET IMPLEMENTED."""

    def __init__(self) -> None:
        raise DriverMissing(
            "LibtmuxDriver is not yet implemented; ticket 02 covers "
            "the local executor. Set MODERATOR_DRIVER=local for "
            "development, or install libtmux and implement this "
            "driver before production deployment."
        )

    def is_alive(self, session: str) -> bool:
        raise DriverMissing("LibtmuxDriver.is_alive: TODO")

    def create_session(self, session: str, command: str) -> None:
        raise DriverMissing("LibtmuxDriver.create_session: TODO")

    def send_keys(self, session: str, text: str) -> None:
        raise DriverMissing("LibtmuxDriver.send_keys: TODO")

    def paste_buffer(self, session: str, text: str) -> None:
        raise DriverMissing("LibtmuxDriver.paste_buffer: TODO")

    def capture_pane(self, session: str, lines: int) -> str:
        raise DriverMissing("LibtmuxDriver.capture_pane: TODO")

    def read_new_bytes(
        self, session: str, since_offset: int
    ) -> tuple[int, bytes]:
        raise DriverMissing("LibtmuxDriver.read_new_bytes: TODO")

    def kill_session(self, session: str) -> None:
        raise DriverMissing("LibtmuxDriver.kill_session: TODO")


__all__ = ["LibtmuxDriver"]