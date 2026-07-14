"""Tests for ``state-inspect`` (default json + jsonl, per ADR-0013)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moderator.cli import main
from moderator.core.models import empty_state
from moderator.state.store import write_state


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own state file path."""
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    # Ensure a fresh empty state exists so jsonl output is non-trivial.
    write_state(empty_state(), state_path)


def test_state_inspect_prints_full_json(capsys: pytest.CaptureResult[str]) -> None:
    rc = main(["state-inspect"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["agents"] == {}
    assert data["actions"] == {}
    assert data["chat"] == []
    assert data["progress"] == {}
    assert data["help_requests"] == []


def test_state_inspect_default_when_state_missing_uses_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureResult[str],
) -> None:
    """If the state file doesn't exist yet, the CLI should still emit
    valid JSON (an empty schema_v1 shape), not error."""
    state_path = tmp_path / "missing.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))

    rc = main(["state-inspect"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1


def test_state_inspect_format_jsonl_emits_ndjson(
    capsys: pytest.CaptureResult[str],
) -> None:
    rc = main(["state-inspect", "--format", "jsonl"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.split("\n") if line]
    assert len(lines) >= 1, "jsonl should emit at least the meta line"
    for line in lines:
        # Every line must be valid JSON on its own (NDJSON contract).
        json.loads(line)


def test_state_inspect_format_jsonl_meta_first(
    capsys: pytest.CaptureResult[str],
) -> None:
    """The first record is the meta line carrying schema_version."""
    rc = main(["state-inspect", "--format", "jsonl"])
    assert rc == 0
    first_line = capsys.readouterr().out.split("\n", 1)[0]
    meta = json.loads(first_line)
    assert meta["kind"] == "meta"
    assert meta["schema_version"] == 1
