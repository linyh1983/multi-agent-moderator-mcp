"""State schema (schema_v1) per ADR-0014.

The root ``ModeratorState`` is the single source of truth — every
collection / metadata field lives under it. ADR-0016 requires
``schema_version`` to bump on incompatible changes; today it is
locked at 1.

Sibling modules (e.g. ``state.store``) import these models; the
glossary in ``docs/glossary.md`` §7 mirrors this file's types for
human reading. The glossary is documentation-only; this file is the
runtime contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_naive() -> datetime:
    """Naive UTC, second-resolution — for JSON-serializable timestamps."""
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


class AgentState(str, Enum):
    """Per ADR-0008, the seven lifecycle states.

    Glossary §4 mirrors this enum as a table. ``str`` mixin so the
    value serializes as its raw string (no ``AgentState.RUNNING``
    surprises in state.json).
    """

    STARTING = "starting"
    RUNNING = "running"
    BLOCKED = "blocked"
    STUCK = "stuck"
    OFFLINE = "offline"
    STOPPED = "stopped"
    ERROR = "error"


class AgentRecord(BaseModel):
    """Per-agent state. Full population lands in ticket 02; ticket 01
    only requires the field names to exist on the schema so that
    future writes do not silently drop data."""

    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    project_dir: str
    role_prompt_path: str | None = None
    tmux_session: str | None = None
    state: AgentState = AgentState.STARTING
    started_at: datetime | None = None
    last_output_at: datetime | None = None
    stopped_at: datetime | None = None
    log_offset: int = 0
    error: str | None = None
    last_error: str | None = None
    # Single-wait invariant (ADR-0009 §4.1): when an agent is
    # ``blocked``, this is the action_id it is waiting for. ``None``
    # for any other state. The agent can be unblocked only by an
    # approve/reject whose id matches this value.
    waiting_for: str | None = None
    additional_agents: list[str] = Field(default_factory=list)


class ApprovalAction(BaseModel):
    """Permanent log entry per ADR-0014 §7.1 — never deleted."""

    id: str
    seq: int
    agent_name: str
    content: str
    status: Literal["pending", "approved", "rejected", "cancelled"]
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    reject_reason: str | None = None
    cancelled_at: datetime | None = None
    cancelled_reason: str | None = None


class ChatMessage(BaseModel):
    """Per ADR-0014 §7.3 — ``kind`` is the routing key for rendering."""

    id: str
    ts: datetime
    kind: Literal["peer", "cue", "system"]
    from_agent: str | None = None
    to_agent: str | None = None
    text: str
    metadata: dict[str, Any] | None = None


class ProgressEntry(BaseModel):
    """Per ADR-0014 §7.7 — kept in a per-agent FIFO cap-50 list."""

    ts: datetime
    text: str


class HelpRequest(BaseModel):
    """Per ADR-0014 §7.4 — cue response removes the matching record."""

    id: str
    agent_name: str
    content: str
    ts: datetime


class ModeratorState(BaseModel):
    """Root state, schema_version 1. See ADR-0014 for the canonical shape
    and ADR-0016 for the migration story."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_at: datetime = Field(default_factory=_utc_now_naive)

    agents: dict[str, AgentRecord] = Field(default_factory=dict)
    actions: dict[str, ApprovalAction] = Field(default_factory=dict)
    chat: list[ChatMessage] = Field(default_factory=list)
    progress: dict[str, list[ProgressEntry]] = Field(default_factory=dict)
    help_requests: list[HelpRequest] = Field(default_factory=list)

    seen_marker_hashes: list[str] = Field(default_factory=list)
    action_seq: int = 0


def empty_state() -> ModeratorState:
    """Return a fresh ``ModeratorState`` matching schema_v1 with
    the five top-level collections empty (per ticket 01 acceptance)."""
    now = _utc_now_naive()
    return ModeratorState(created_at=now, updated_at=now)


__all__ = [
    "AgentState",
    "AgentRecord",
    "ApprovalAction",
    "ChatMessage",
    "ProgressEntry",
    "HelpRequest",
    "ModeratorState",
    "empty_state",
]
