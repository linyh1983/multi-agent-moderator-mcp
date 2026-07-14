"""``moderator state-inspect`` subcommand.

Two output formats (ADR-0013 §5.5):

- ``json``  (default): pretty-printed JSON of the entire state file.
- ``jsonl``: one JSON object per record across all five top-level
  collections. Pipes cleanly through ``jq`` / ``grep``.

Filters: ``--agent`` narrows to records related to one agent;
``--since`` / ``--until`` apply where a record has a timestamp.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from moderator.core.models import ModeratorState, empty_state
from moderator.state.store import default_state_path, read_state


# ---------------------------------------------------------------------------
# JSONL record shaping — every record is a flat dict keyed by record kind.
# ---------------------------------------------------------------------------


def _records(
    state: ModeratorState,
    *,
    agent: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def passes_ts(ts: datetime | None) -> bool:
        if since and ts and ts < since:
            return False
        if until and ts and ts > until:
            return False
        return True

    def passes_agent(name: str | None) -> bool:
        return not agent or name == agent

    # agents
    for name, rec in state.agents.items():
        if not passes_agent(name) or not passes_ts(rec.started_at):
            continue
        out.append({"kind": "agent", **rec.model_dump(mode="json")})

    # actions
    for action_id, action in state.actions.items():
        if not passes_agent(action.agent_name) or not passes_ts(action.created_at):
            continue
        out.append({"kind": "action", **action.model_dump(mode="json")})

    # chat (peer / cue / system)
    for msg in state.chat:
        if not passes_agent(msg.from_agent) and not passes_agent(msg.to_agent):
            # system messages may have no agents; let timestamp filter decide
            if msg.from_agent is not None or msg.to_agent is not None:
                continue
        if not passes_ts(msg.ts):
            continue
        out.append({"kind": "chat", **msg.model_dump(mode="json")})

    # progress (per agent list)
    for name, entries in state.progress.items():
        if not passes_agent(name):
            continue
        for entry in entries:
            if not passes_ts(entry.ts):
                continue
            out.append(
                {
                    "kind": "progress",
                    "agent_name": name,
                    **entry.model_dump(mode="json"),
                }
            )

    # help_requests
    for req in state.help_requests:
        if not passes_agent(req.agent_name) or not passes_ts(req.ts):
            continue
        out.append({"kind": "help_request", **req.model_dump(mode="json")})

    # Top-level meta line first — schema_version etc. is what callers need first.
    out.append(
        {
            "kind": "meta",
            "schema_version": state.schema_version,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "action_seq": state.action_seq,
        }
    )
    return out


def _parse_iso(s: str) -> datetime:
    """Accept common ISO-8601 forms. Z suffix tolerated."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Public entry point — invoked by ``moderator.cli.main``.
# ---------------------------------------------------------------------------


def run(
    *,
    output_format: str,
    agent: str | None,
    since: str | None,
    until: str | None,
) -> int:
    """Run state-inspect. Returns shell exit code."""
    path = default_state_path()
    state = read_state(path) if path.exists() else empty_state()

    since_dt = _parse_iso(since) if since else None
    until_dt = _parse_iso(until) if until else None

    if output_format == "json":
        payload = state.model_dump(mode="json")
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    if output_format == "jsonl":
        records = _records(
            state, agent=agent, since=since_dt, until=until_dt
        )
        for record in records:
            json.dump(record, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        return 0

    sys.stderr.write(f"unknown format: {output_format!r}\n")
    return 2


__all__ = ["run"]
