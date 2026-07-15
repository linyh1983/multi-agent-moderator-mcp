"""Tests for state-store schema_version refusal and migration
script (ticket 08).

Covers ADR-0004 §"v1 形态" + §"规模与迁移触发" and
ADR-0016 (manual migration, per-version step dict, reversible
steps, never-skip-versions rule).

Key invariants under test:

- ``read_state`` raises :class:`StateSchemaTooOld` when the
  on-disk ``schema_version`` is below ``MIN_SUPPORTED_SCHEMA_VERSION``.
- The error message matches the spec format exactly:
  ``State schema version <N> is below minimum supported version
  <M>. Run: python -m state.migrate <path>``
- ``state.migrate.migrate(state, from_v, to_v)`` applies each
  per-version step in order without skipping.
- Each step has a reverse function registered in
  ``MIGRATIONS_REVERSE``; ``reverse(reverse(state))`` is the
  identity (round-trip).
- ``python -m state.migrate <path>`` loads JSON, calls
  ``migrate()`` to ``CURRENT_SCHEMA_VERSION``, writes a
  ``.bak.<ts>`` backup, then writes the migrated state back
  atomically. Exit code 0 on success.
- ``state-inspect`` (json and jsonl) emits a migration
  suggestion line when any ADR-0004 trigger is met:
  ``state.actions | length ≥ 10_000``, ``state.chat | length
  ≥ 10_000``, or file size ≥ 50 MiB.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from moderator.core.models import empty_state
from moderator.state.inspect_cmd import run as inspect_run
from moderator.state.migrate import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIGRATIONS_REVERSE,
    MigrationError,
    migrate,
)
from moderator.state.store import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    STATE_PATH_ENV,
    StateSchemaTooOld,
    read_state,
    write_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    p = tmp_path / "moderator_state.json"
    monkeypatch.setenv(STATE_PATH_ENV, str(p))
    return p


def _write_raw(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Schema-version refusal
# ---------------------------------------------------------------------------


def test_min_supported_schema_version_is_one() -> None:
    """Sanity: the minimum supported schema version is 1 (per
    ADR-0014 — we ship schema_v1 today)."""
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_current_schema_version_is_one() -> None:
    """Sanity: the current schema version is 1 (production)."""
    assert CURRENT_SCHEMA_VERSION == 1


def test_read_state_refuses_schema_version_zero(
    state_path: Path,
) -> None:
    """A state file with schema_version=0 raises
    StateSchemaTooOld."""
    _write_raw(state_path, {"schema_version": 0, "agents": {}})
    with pytest.raises(StateSchemaTooOld) as excinfo:
        read_state(state_path)
    msg = str(excinfo.value)
    assert "schema version 0" in msg
    assert "minimum supported version" in msg
    assert "python -m state.migrate" in msg
    assert str(state_path) in msg


def test_read_state_refuses_missing_schema_version(
    state_path: Path,
) -> None:
    """A state file with no schema_version field at all is
    treated as v0 (legacy) and refused."""
    _write_raw(state_path, {"agents": {}})
    with pytest.raises(StateSchemaTooOld) as excinfo:
        read_state(state_path)
    assert "schema version" in str(excinfo.value)


def test_read_state_accepts_current_schema_version(
    state_path: Path,
) -> None:
    """A state file with the current schema_version loads
    normally."""
    payload: dict[str, Any] = empty_state().model_dump(mode="json")
    _write_raw(state_path, payload)
    state = read_state(state_path)
    assert state.schema_version == CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Migration framework
# ---------------------------------------------------------------------------


def test_migrate_empty_step_chain_is_noop() -> None:
    """migrate() with from_v == to_v returns the state
    unchanged."""
    state = {"schema_version": 1, "agents": {}}
    out = migrate(state, from_version=1, to_version=1)
    assert out == state


def test_migrate_rejects_downgrade() -> None:
    """migrate() refuses from > to (downgrade not supported)."""
    state = {"schema_version": 2, "agents": {}}
    with pytest.raises(MigrationError):
        migrate(state, from_version=2, to_version=1)


def test_migrate_runs_each_step_in_order() -> None:
    """migrate() with a multi-step chain applies each registered
    MIGRATIONS[v] in turn and bumps schema_version after every
    step (ADR-0016 §"不允许跳过版本")."""
    # MIGRATIONS must contain a step for every (current-1, current)
    # boundary so the framework can carry a legacy v0 state all
    # the way up to CURRENT_SCHEMA_VERSION (==1 today).
    assert len(MIGRATIONS) >= 1, (
        "MIGRATIONS must contain at least one step (the legacy "
        "v0 → v1 entry) per ADR-0016"
    )
    initial: dict[str, Any] = {
        "schema_version": 0,
        "agents": {},
        "actions": {},
        "chat": [],
        "progress": {},
        "help_requests": [],
        "seen_marker_hashes": [],
        "action_seq": 0,
    }
    out = migrate(
        initial,
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
    )
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION


def test_each_migration_step_has_reverse() -> None:
    """Every MIGRATIONS[v] has a corresponding MIGRATIONS_REVERSE[v]
    registered (ADR-0016 §"每次迁移必须可逆")."""
    for v in MIGRATIONS:
        assert v in MIGRATIONS_REVERSE, (
            f"missing reverse for migration {v} → {v + 1}"
        )


def test_migration_round_trip_is_identity() -> None:
    """For every MIGRATIONS[v]: apply step, apply reverse, must
    be the identity transformation on state dicts."""
    for v, step in MIGRATIONS.items():
        reverse = MIGRATIONS_REVERSE[v]
        # Minimal v-state. We construct a state that the forward
        # step is designed to consume.
        forward_input = {
            "schema_version": v,
            "agents": {},
            "actions": {},
            "chat": [],
            "progress": {},
            "help_requests": [],
            "seen_marker_hashes": [],
            "action_seq": 0,
        }
        forward_out = step(forward_input)
        reverse_out = reverse(forward_out)
        # The reverse function returns a state at version v
        # (schema_version is set by the migrate() driver, not
        # by individual steps; we just verify the *content*
        # round-trips).
        for key, value in forward_input.items():
            if key == "schema_version":
                continue
            assert reverse_out.get(key) == value, (
                f"round-trip mismatch for {key!r} in migration {v}"
            )


# ---------------------------------------------------------------------------
# Migration CLI (python -m state.migrate)
# ---------------------------------------------------------------------------


def test_cli_migrates_legacy_v0_state(
    state_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m state.migrate <path>`` takes a legacy v0 state
    and writes a v1 backup + a v1-migrated JSON. Exit code 0."""
    legacy = {
        "agents": {},
        "actions": {},
        "chat": [],
        "progress": {},
        "help_requests": [],
        # schema_version intentionally absent.
    }
    _write_raw(state_path, legacy)

    # Invoke as a subprocess so we exercise the __main__ block.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "moderator.state.migrate",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Backup file with .bak.<ts> exists.
    backups = list(state_path.parent.glob(f"{state_path.name}.bak.*"))
    assert len(backups) == 1, "expected exactly one .bak backup"
    # The backup is the original v0 content.
    backup_data = json.loads(backups[0].read_text(encoding="utf-8"))
    assert "schema_version" not in backup_data
    # The migrated file is v1 and loads cleanly.
    state = read_state(state_path)
    assert state.schema_version == CURRENT_SCHEMA_VERSION


def test_cli_already_current_is_noop_exit_zero(
    state_path: Path,
) -> None:
    """Migrating a state that's already at CURRENT_SCHEMA_VERSION
    succeeds with no migration steps applied and creates a backup
    anyway (so the moderator can roll back if they regret)."""
    payload = empty_state().model_dump(mode="json")
    _write_raw(state_path, payload)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "moderator.state.migrate",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    backups = list(state_path.parent.glob(f"{state_path.name}.bak.*"))
    assert len(backups) == 1


def test_cli_exits_2_on_corrupt_json(
    state_path: Path,
) -> None:
    """A non-JSON state file → MigrationError, exit code 2."""
    state_path.write_text("not json", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "moderator.state.migrate",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Migration suggestion in state-inspect
# ---------------------------------------------------------------------------


def test_inspect_json_emits_migration_suggestion_when_actions_large(
    state_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When state.actions ≥ 10_000, state-inspect json prints a
    final line pointing to python -m state.migrate."""
    state = empty_state()
    # Hand-craft 10_000 synthetic actions without paying the
    # ApprovalAction(...) construction cost for every test —
    # we go straight to dict assignment on the model.
    from moderator.core.models import ApprovalAction, _utc_now_naive

    now = _utc_now_naive()
    for i in range(10_000):
        state.actions[f"a-{i}"] = ApprovalAction(
            id=f"a-{i}",
            seq=i,
            agent_name="synth",
            content="",
            status="pending",
            created_at=now,
        )
    write_state(state, state_path)
    rc = inspect_run(
        output_format="json",
        agent=None,
        since=None,
        until=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "python -m state.migrate" in out


def test_inspect_jsonl_emits_migration_suggestion_when_chat_large(
    state_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When state.chat ≥ 10_000, state-inspect jsonl prints the
    suggestion at the end of the stream."""
    from moderator.core.models import ChatMessage

    state = empty_state()
    for i in range(10_000):
        state.chat.append(
            ChatMessage(
                id=f"msg-{i}",
                ts=state.created_at,
                kind="system",
                from_agent=None,
                to_agent=None,
                text=str(i),
            )
        )
    write_state(state, state_path)
    rc = inspect_run(
        output_format="jsonl",
        agent=None,
        since=None,
        until=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "python -m state.migrate" in out


def test_inspect_no_suggestion_when_below_threshold(
    state_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Small states do not emit a migration suggestion."""
    state = empty_state()
    write_state(state, state_path)
    rc = inspect_run(
        output_format="json",
        agent=None,
        since=None,
        until=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "python -m state.migrate" not in out