"""``cue`` MCP tool (ticket 10).

Send a directive from the moderator into a running agent's tmux
session. The agent sees ``<moderator-cue>{message}</moderator-cue>``
on its stdin (followed by an Enter) and the directive is logged as
a :class:`ChatMessage` with ``kind="cue"``.

Semantics come from ADR-0005 §"symmetry", ADR-0009 §4.1
(single-wait invariant), and ADR-0013 §5.4 (per-agent rate limit):

- ``target="moderator"`` is forbidden — only the moderator can use
  it, and there's no moderator agent to deliver to.
- ``target="all"`` fans out to every agent in :attr:`AgentState.RUNNING`.
  BLOCKED / STOPPED / ERROR / OFFLINE are silently skipped — the
  moderator cares about agents they can actually reach.
- For a named target, only :attr:`AgentState.RUNNING` or
  :attr:`AgentState.BLOCKED` accept cues. STOPPED / ERROR /
  OFFLINE error out with the state name in the message so the
  operator can fix it.
- Cues to a BLOCKED agent are DELIVERED but do NOT clear the
  blocking action (cue ≠ approve/reject).
- Role-label targets (e.g. ``cue(target="reviewer", ...)``) are
  rejected with "role-label cues not yet supported" — out of
  scope for ticket 10.
- Per-agent rate limit: at most ``1 / CUE_GAP_SECONDS`` cues per
  agent per second (sender-side throttle). The cue message itself
  is not counted as a marker (cue is moderator→agent, not
  agent→moderator); the throttle exists to avoid flooding tmux
  buffers when a moderator loops a cue script.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from mcp.types import CallToolResult, Tool

from moderator.core.models import (
    AgentRecord,
    AgentState,
    ChatMessage,
    ModeratorState,
    _utc_now_naive,
)
from moderator.drivers import DriverError, TmuxDriver
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error, make_text_result


_log = logging.getLogger(__name__)


# Per-agent sender-side throttle (ADR-0013 §5.4). 10/s matches the
# marker rate limit the worker enforces (defaults line up); tests
# pass ``CUE_GAP_SECONDS=0`` to disable the throttle and stay fast.
CUE_GAP_SECONDS: float = 0.1

# Wrap for the on-the-wire body. The literal angle-bracket names
# are how agents parse "this came from the moderator" (matches
# <moderator-approve> / <moderator-reject> on the agent parser).
_WRAP_OPEN = "<moderator-cue>"
_WRAP_CLOSE = "</moderator-cue>"


# In-memory per-agent last-cue dispatch clock. Module-level so the
# handler (which is otherwise stateless) can share across calls.
# Lost on restart — acceptable per ADR-0017 (no auto-restart).
_last_cue_mono: dict[str, float] = {}


TOOL = Tool(
    name="cue",
    description=(
        "Send a directive (cue) to one agent, a role label, or "
        "'all' running agents. The message is wrapped in "
        "<moderator-cue>...</moderator-cue> and typed into the "
        "target's tmux session, and a ChatMessage (kind='cue') is "
        "appended to the chat history. Cues to BLOCKED agents are "
        "delivered but do NOT unblock the agent."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Agent name, 'all' to fan out to every RUNNING "
                    "agent, 'moderator' (forbidden), or a role label "
                    "(not yet supported)."
                ),
            },
            "message": {
                "type": "string",
                "description": "Directive body to send.",
            },
        },
        "required": ["target", "message"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------------------
# Helpers — exposed for tests (private name but module-level).
# ---------------------------------------------------------------------------


def reset_cue_throttle() -> None:
    """Clear the per-agent last-cue clock.

    Tests call this in setup so one test's throttle history does
    not slow the next test. Production never needs this.
    """
    _last_cue_mono.clear()


def _enforce_cue_rate_limit(name: str, *, gap: float = CUE_GAP_SECONDS) -> None:
    """Sleep until ``gap`` seconds have passed since this agent's
    last cue dispatch (ADR-0013 §5.4). No-op when ``gap <= 0`` so
    tests stay fast.

    Each agent's clock is independent so a cue burst to agent A
    does not throttle agent B.
    """
    if gap <= 0:
        return
    last = _last_cue_mono.get(name)
    now_mono = time.monotonic()
    if last is not None:
        elapsed = now_mono - last
        if elapsed < gap:
            time.sleep(gap - elapsed)
    _last_cue_mono[name] = time.monotonic()


def _validate_arguments(
    arguments: dict[str, Any],
) -> tuple[str, str] | CallToolResult:
    """Return (target, message) on success, or an error result.

    Both must be non-empty strings — same shape as the JSON
    schema enforced at the MCP layer, but we re-check because
    tool callers come from JSON-RPC and may pass junk.
    """
    target = arguments.get("target")
    message = arguments.get("message")

    if not isinstance(target, str):
        return make_error("cue: 'target' must be a non-empty string")
    if not target:
        return make_error("cue: 'target' must be a non-empty string")
    if not isinstance(message, str):
        return make_error("cue: 'message' must be a non-empty string")
    if not message:
        return make_error("cue: 'message' must be a non-empty string")
    return target, message


def _resolve_target(
    target: str, state: ModeratorState
) -> list[AgentRecord] | CallToolResult:
    """Return the list of agents to which ``target`` resolves.

    - ``"moderator"`` → forbidden error (ADR-0005 symmetry).
    - ``"all"``       → every RUNNING agent (BLOCKED / STOPPED /
      ERROR / OFFLINE silently skipped).
    - role label (anything that's not a known agent name and
      not one of the reserved strings above) → "not yet supported"
      error. We detect role-label status by simple check: it's
      a role label iff it's not a known agent name and not one of
      the reserved strings.
    - known agent name → single-element list, or a state error
      if the agent is in STOPPED / ERROR / OFFLINE.

    Note on role-label detection: there's no separate "roles"
    registry yet — every string that isn't an exact agent name
    AND isn't a reserved literal is treated as a role label. This
    matches the ticket scope (v1: single agent + 'all' only).
    """
    if target == "moderator":
        return make_error("cue: target 'moderator' is forbidden")

    if target == "all":
        return [
            rec
            for rec in state.agents.values()
            if rec.state is AgentState.RUNNING
        ]

    rec = state.agents.get(target)
    if rec is None:
        # v1 cannot distinguish a typo from a role label — the
        # only way an operator adds a role label is by registering
        # a new agent, which then appears in state.agents. So any
        # unknown target is either a typo or an unsupported
        # role label; both render the same error mentioning the
        # role-label limitation so the moderator knows what's
        # available (out-of-scope per ticket 10).
        return make_error(
            f"cue: agent {target!r} not found (role-label cues not "
            f"yet supported — see ticket 10)"
        )

    if rec.state in (AgentState.STOPPED, AgentState.ERROR, AgentState.OFFLINE):
        return make_error(
            f"cue: agent {target!r} is in state {rec.state.value!r} "
            f"— cannot deliver"
        )

    # RUNNING or BLOCKED — both accept cues (BLOCKED does NOT unblock).
    return [rec]


def _send_cue(tmux: TmuxDriver, session: str, message: str) -> None:
    """Send the wrapped message into ``session``. DriverError is
    swallowed: the moderator would rather see a chat entry than a
    tool-level error, and the operator can re-cue if the agent
    missed the first one (matches approve / reject)."""
    body = f"{_WRAP_OPEN}{message}{_WRAP_CLOSE}"
    try:
        tmux.send_keys(session, body)
    except DriverError as exc:
        # Driver errors are swallowed (matches approve / reject):
        # the chat entry is already persisted, and the moderator
        # can re-cue if the agent missed it.
        _log.warning("cue: send_keys failed for %r: %s", session, exc)


def _record_cue(
    state: ModeratorState, agent: AgentRecord, message: str
) -> None:
    """Append a single :class:`ChatMessage` for the delivered cue.

    Mutates ``state.chat`` in place; the caller is responsible for
    writing the state back to disk.
    """
    state.chat.append(
        ChatMessage(
            id=f"msg-cue-{uuid4().hex[:12]}",
            ts=_utc_now_naive(),
            kind="cue",
            from_agent=None,  # moderator → agent; sender not recorded
            to_agent=agent.name,
            text=message,
        )
    )


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """MCP handler — see module docstring for full semantics."""
    validated = _validate_arguments(arguments)
    if isinstance(validated, CallToolResult):
        return validated
    target, message = validated

    state = read_state()
    resolved = _resolve_target(target, state)
    if isinstance(resolved, CallToolResult):
        return resolved
    recipients = resolved
    if not recipients:
        return make_error(
            f"cue: no RUNNING agents found — message {message!r} not sent"
        )

    # Acquire the tmux driver lazily so a runtime with no
    # configured drivers surfaces as a clean DriverError rather
    # than an import-time crash. The runtime `set_drivers` path
    # injects a LocalExecutor pair by default.
    from moderator.runtime import get_drivers

    _, tmux = get_drivers()

    delivered: list[str] = []
    for rec in recipients:
        # Per-agent throttle BEFORE we touch tmux — flooding one
        # agent should not delay the others.
        _enforce_cue_rate_limit(rec.name)
        session = rec.tmux_session or f"mod-{rec.name}"
        _send_cue(tmux, session, message)
        _record_cue(state, rec, message)
        delivered.append(rec.name)

    write_state(state)
    return make_text_result(
        f"cue: sent to {len(delivered)} agent(s): {', '.join(delivered)}"
    )


__all__ = [
    "CUE_GAP_SECONDS",
    "TOOL",
    "handle",
    "reset_cue_throttle",
]
