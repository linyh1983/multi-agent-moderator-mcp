"""Persistent state store for Moderator MCP Server.

Conforms to ADR-0004 (JSON + cross-process file lock) and ADR-0014
(schema_v1 — see ``core.models.ModeratorState``). Atomic write:
temp file → fsync → ``os.replace`` (atomic on both POSIX and
Windows). Cross-process lock uses an atomic-take-lockfile
algorithm (``os.link`` on POSIX, ``os.rename`` on Windows) — this
is reliable and known to work; ``msvcrt.locking`` is best-effort
only and does not reliably block cross-process on Windows.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from moderator.core.models import ModeratorState, empty_state


STATE_PATH_ENV = "MODERATOR_STATE_PATH"
_LOCK_BACKOFF_INITIAL = 0.01
_LOCK_BACKOFF_MAX = 0.5


# Schema-version envelope (ADR-0016). ``MIN_SUPPORTED_SCHEMA_VERSION``
# is the floor below which :func:`read_state` refuses to load —
# moderators must run ``python -m state.migrate`` first. Bump
# the floor whenever a migration step becomes mandatory before
# load.
MIN_SUPPORTED_SCHEMA_VERSION: int = 1
CURRENT_SCHEMA_VERSION: int = 1


def default_state_path() -> Path:
    """Return the default state file path, overrideable via env var."""
    override = os.environ.get(STATE_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".moderator" / "moderator_state.json"


class StateCorrupt(Exception):
    """Raised when the state file on disk is unreadable or fails validation."""


class StateSchemaTooOld(Exception):
    """Raised when the state file's ``schema_version`` is below
    ``MIN_SUPPORTED_SCHEMA_VERSION``. Moderators must run
    ``python -m state.migrate <path>`` to upgrade.

    Per ADR-0016 the loader refuses to silently migrate on
    startup — schema changes are moderator-visible so they can
    audit and roll back.
    """


# ---------------------------------------------------------------------------
# Cross-process lock — atomic-take-lockfile
# ---------------------------------------------------------------------------
#
# Algorithm:
#   1. Create a unique handle file in the same directory as the state file.
#   2. Atomically "take" the canonical lock path by either:
#        - POSIX:  os.link(handle, lock_path)            — link fails EEXIST
#        - Windows: os.rename(handle, lock_path)         — raises FileExistsError
#      whichever the platform supports natively.
#   3. On contention, back off and retry.
#   4. Release: unlink the canonical lock path.
#
# The canonical lock file's existence *is* the lock — no byte-range locking,
# no fcntl dance, no ctypes. If a process crashes, the lock file is left
# behind; this is acceptable for ticket 01's MVP scope (per ADR-0004
# "single moderator + last-write-wins").


def _atomic_take(src: str, dst: str) -> None:
    """Atomically link/rename ``src`` over ``dst``. Raises ``FileExistsError``
    (or OSError with EEXIST) if ``dst`` already exists — that is the lock
    contention signal."""
    if sys.platform == "win32":
        # Windows os.rename raises FileExistsError if dst exists. atomic.
        os.rename(src, dst)
    else:
        # POSIX os.link: creates a hard link, fails with EEXIST if dst exists.
        # Atomic with respect to other processes. We then delete the temp.
        os.link(src, dst)


class StateLock:
    """Cross-process exclusive file lock (context manager)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.parent / f"{path.name}.lock"
        self._handle: Path | None = None

    def __enter__(self) -> StateLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create a private handle file in the same dir so os.rename /
        # os.link are atomic (same filesystem).
        fd, handle_str = tempfile.mkstemp(
            prefix=f".{self._lock_path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        os.close(fd)
        # Truncate — mkstemp leaves 0 bytes, that's fine. Keep handle path.
        self._handle = Path(handle_str)

        backoff = _LOCK_BACKOFF_INITIAL
        while True:
            try:
                _atomic_take(str(self._handle), str(self._lock_path))
                return self
            except FileExistsError:
                # Lock held by another process — backoff and retry.
                pass
            except OSError as exc:
                # Some platforms surface EEXIST as OSError. Match it.
                if getattr(exc, "errno", None) != 17 and getattr(exc, "winerror", None) != 80:
                    raise
            time.sleep(backoff)
            backoff = min(backoff * 2, _LOCK_BACKOFF_MAX)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            os.unlink(str(self._lock_path))
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort — if we can't unlink, leave it; next acquire
            # will block until something unlinks (process restart etc).
            pass
        if self._handle is not None:
            # On POSIX, _atomic_take via os.link leaves the handle intact.
            # On Windows, os.rename consumed it. Both branches are safe.
            try:
                os.unlink(str(self._handle))
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._handle = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def state_lock(path: Path | None = None) -> Iterator[Path]:
    """Hold the cross-process state lock for the duration of the block.

    Yields the resolved state path so callers don't need to repeat it.
    """
    if path is None:
        path = default_state_path()
    with StateLock(path):
        yield path


def read_state(path: Path | None = None) -> ModeratorState:
    """Read state from ``path``. Returns empty state if file absent.

    Acquires the cross-process lock for the duration of the read.
    """
    if path is None:
        path = default_state_path()
    with StateLock(path):
        return _read_unlocked(path)


def write_state(state: ModeratorState, path: Path | None = None) -> None:
    """Atomically write ``state`` to ``path`` (or default).

    Steps: hold exclusive lock → write temp file in the same directory
    → fsync → ``os.replace`` into ``path``. Atomic on POSIX and Windows.
    """
    if path is None:
        path = default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with StateLock(path):
        _write_unlocked(state, path)


# ---------------------------------------------------------------------------
# unlocked internals (callers must hold the lock)
# ---------------------------------------------------------------------------


def _read_unlocked(path: Path) -> ModeratorState:
    if not path.exists():
        return empty_state()

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorrupt(
            f"state file at {path} is not valid JSON: {exc}"
        ) from exc

    # ADR-0016: refuse to load below the minimum supported
    # schema version. Legacy state files (schema_version=0 or
    # absent) must be migrated first via ``state.migrate``.
    found_version = data.get("schema_version", 0)
    try:
        found_version = int(found_version)
    except (TypeError, ValueError):
        found_version = 0
    if found_version < MIN_SUPPORTED_SCHEMA_VERSION:
        raise StateSchemaTooOld(
            f"State schema version {found_version} is below "
            f"minimum supported version "
            f"{MIN_SUPPORTED_SCHEMA_VERSION}. "
            f"Run: python -m state.migrate {path}"
        )

    try:
        return ModeratorState.model_validate(data)
    except ValidationError as exc:
        raise StateCorrupt(
            f"state file at {path} failed schema_v1 validation: {exc}"
        ) from exc


def _write_unlocked(state: ModeratorState, path: Path) -> None:
    payload = state.model_dump(mode="json")
    body = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "STATE_PATH_ENV",
    "StateCorrupt",
    "StateLock",
    "StateSchemaTooOld",
    "default_state_path",
    "read_state",
    "state_lock",
    "write_state",
]
