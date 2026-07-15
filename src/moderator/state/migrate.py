"""State-file schema migration framework.

Per ADR-0016:

- ``MIGRATIONS[v]`` is a function that takes a state dict at
  schema version ``v`` and returns it at version ``v + 1``.
- ``MIGRATIONS_REVERSE[v]`` is the inverse: takes a state dict
  at version ``v + 1`` back to ``v``. Required for rollback
  and tested via round-trip.
- ``migrate(state, from_version, to_version)`` walks the chain
  without skipping versions.
- ``python -m state.migrate <path>`` loads the JSON, calls
  ``migrate()`` to ``CURRENT_SCHEMA_VERSION``, writes a
  ``.bak.<ts>`` backup, then writes the migrated state back
  atomically.

Schema version 1 is current production (ADR-0014). ``MIGRATIONS``
is seeded with a single legacy entry (``0 → 1``) so legacy
state files without a ``schema_version`` field can be promoted.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from moderator.core.models import ModeratorState
from moderator.state.store import (
    CURRENT_SCHEMA_VERSION,
    StateLock,
    default_state_path,
)


# Re-export so callers can read these names from either module.
__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "MIGRATIONS_REVERSE",
    "MigrationError",
    "migrate",
]


class MigrationError(Exception):
    """Raised when a state migration step fails or is
    requested in an invalid direction."""


# ---------------------------------------------------------------------------
# Per-version step dict (ADR-0016)
# ---------------------------------------------------------------------------


def _legacy_v0_to_v1(state: dict[str, Any]) -> dict[str, Any]:
    """Promote a legacy v0 state (no ``schema_version`` field)
    to v1. Field shapes haven't changed; we only ensure the
    top-level keys that ADR-0014 requires are present."""
    state.setdefault("agents", {})
    state.setdefault("actions", {})
    state.setdefault("chat", [])
    state.setdefault("progress", {})
    state.setdefault("help_requests", [])
    state.setdefault("seen_marker_hashes", [])
    state.setdefault("action_seq", 0)
    return state


def _v1_to_v0(state: dict[str, Any]) -> dict[str, Any]:
    """Reverse of :func:`_legacy_v0_to_v1`. Drops keys that
    weren't in the v0 schema and strips ``schema_version``."""
    state.pop("schema_version", None)
    return state


MIGRATIONS: dict[int, Any] = {
    0: _legacy_v0_to_v1,
}

MIGRATIONS_REVERSE: dict[int, Any] = {
    0: _v1_to_v0,
}


# ---------------------------------------------------------------------------
# Migration driver
# ---------------------------------------------------------------------------


def migrate(
    state: dict[str, Any],
    *,
    from_version: int,
    to_version: int,
) -> dict[str, Any]:
    """Walk ``state`` from ``from_version`` to ``to_version`` by
    applying each registered step in order (ADR-0016).

    - ``from_version == to_version``: no-op, returns ``state``
      untouched.
    - ``from_version > to_version``: raises
      :class:`MigrationError` (downgrade is not supported by
      this driver — moderators who want to downgrade should
      use the per-step REVERSE functions manually).
    - A missing step in the chain raises :class:`MigrationError`
      so we never silently skip versions.
    """
    if from_version > to_version:
        raise MigrationError(
            f"cannot migrate downward: from_version={from_version} "
            f"> to_version={to_version}"
        )

    if from_version == to_version:
        return state

    for v in range(from_version, to_version):
        step = MIGRATIONS.get(v)
        if step is None:
            raise MigrationError(
                f"no migration step registered for v{v} → v{v + 1}; "
                f"refusing to skip versions"
            )
        state = step(state)
        state["schema_version"] = v + 1
    return state


# ---------------------------------------------------------------------------
# CLI: python -m state.migrate <path>
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MigrationError(
            f"state file at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MigrationError(
            f"state file at {path} is not a JSON object at the "
            f"top level (got {type(data).__name__})"
        )
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically. We hold the
    cross-process lock so a concurrent MCP server can't read
    a partial file."""
    body = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    with StateLock(path):
        # Use the same atomic-write pattern as moderator.state.store
        # but at the dict level (we don't have a ModeratorState
        # object yet — the migration may add new fields the
        # current model doesn't know about).
        import os
        import tempfile

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            try:
                os.write(fd, body.encode("utf-8"))
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


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m state.migrate [<path>]``.

    - ``<path>``: target state file. Defaults to
      :func:`default_state_path`.
    - Returns shell exit code (0 on success).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        sys.stderr.write(
            "usage: python -m state.migrate [<path>]\n"
        )
        return 2

    path = Path(args[0]) if args else default_state_path()

    if not path.exists():
        sys.stderr.write(f"state file not found: {path}\n")
        return 2

    # Load.
    try:
        data = _load_json(path)
    except MigrationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    from_v = int(data.get("schema_version", 0))
    to_v = CURRENT_SCHEMA_VERSION

    # Backup the original (ADR-0016: backup before mutating).
    ts = time.strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{ts}")
    try:
        backup_path.write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError as exc:
        sys.stderr.write(f"failed to write backup {backup_path}: {exc}\n")
        return 1

    # Migrate.
    try:
        migrated = migrate(data, from_version=from_v, to_version=to_v)
    except MigrationError as exc:
        sys.stderr.write(f"migration failed: {exc}\n")
        return 1

    # Sanity: model_validate passes (catches schema drift).
    try:
        ModeratorState.model_validate(migrated)
    except Exception as exc:  # noqa: BLE001 — Pydantic raises broad
        sys.stderr.write(
            f"migrated state failed schema validation: {exc}\n"
        )
        return 1

    # Write back atomically.
    try:
        _atomic_write_json(path, migrated)
    except OSError as exc:
        sys.stderr.write(f"failed to write migrated state: {exc}\n")
        return 1

    sys.stdout.write(
        f"migrated {path}: schema_version {from_v} -> {to_v}\n"
        f"backup: {backup_path}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_cli())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def migration_suggestion(
    state: ModeratorState, path: Path | None = None
) -> str | None:
    """Return the migration-suggestion one-liner for ``state``
    when ADR-0004 size triggers are met, otherwise None.

    Triggers (any one):

    - ``len(state.actions) ≥ 10_000``
    - ``len(state.chat) ≥ 10_000``
    - on-disk file size ≥ 50 MiB (only checked when ``path``
      is supplied).
    """
    threshold_actions = 10_000
    threshold_chat = 10_000
    threshold_bytes = 50 * 1024 * 1024  # 50 MiB

    triggers: list[str] = []
    if len(state.actions) >= threshold_actions:
        triggers.append(
            f"state.actions has {len(state.actions)} entries "
            f"(≥ {threshold_actions})"
        )
    if len(state.chat) >= threshold_chat:
        triggers.append(
            f"state.chat has {len(state.chat)} entries "
            f"(≥ {threshold_chat})"
        )
    if path is not None and path.exists():
        size = path.stat().st_size
        if size >= threshold_bytes:
            triggers.append(
                f"state file is {size} bytes (≥ {threshold_bytes})"
            )

    if not triggers:
        return None

    target = str(path or default_state_path())
    return (
        "Run: python -m state.migrate " + target
        + "  # " + "; ".join(triggers)
    )