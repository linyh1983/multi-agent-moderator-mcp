"""``start_session`` MCP tool (ticket 02; ticket 05 extends it).

Flow:

1. Validate inputs.
2. **Same-name reject** (ADR-0005 §6.5): if ``state.agents`` already
   contains ``name``, return ``AgentAlreadyExists`` and DO NOT
   touch state.
3. Acquire the process driver pair (default: :class:`LocalExecutor`).
4. Stage ``role_prompt`` to a temp file on the moderator host
   (never persisted into state.json — only the path is).
5. SFTP it to ``/tmp/moderator/role-<name>.txt`` on the agent host.
6. Create a tmux session ``mod-<name>`` that runs ``claude`` with
   the role prompt piped in. Per ADR-0003 the agent process owns
   its role; the moderator never embeds role text in state.
7. Update ``state.agents[name]``:
   - ``role_prompt_path`` set to the remote path.
   - ``tmux_session`` set to ``mod-<name>``.
   - ``state`` transitions STARTING → RUNNING.
   - ``started_at``, ``last_output_at`` set to now (UTC, naive).
   - ``log_offset`` initialized to 0.
   - ``additional_agents`` recorded verbatim from the call (ADR-0005
     §6.2 one-way allowlist).
8. On any failure: write an error record with a descriptive
   ``error`` string. role_prompt text never appears in state.

Driver injection:

- Production: :func:`moderator.runtime.get_drivers` is called
  on demand. The choice is controlled by ``MODERATOR_DRIVER``
  (``local`` or ``ssh``; defaults to ``local``).
- Tests: :func:`moderator.runtime.set_drivers` overrides before
  :func:`handle` is called.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, Tool

from moderator.core.models import AgentRecord, AgentState, _utc_now_naive
from moderator.drivers import DriverError, DriverMissing
from moderator.drivers import SshDriver, TmuxDriver
from moderator.runtime import get_drivers
from moderator.state.store import read_state, write_state
from moderator.tools._results import make_error, make_text_result


TOOL = Tool(
    name="start_session",
    description=(
        "Start a remote agent session. Validates inputs, stages the role "
        "prompt on the agent host, and creates a tmux session 'mod-<name>' "
        "running the agent. On success the state transitions "
        "starting → running within the same call. Same-name reuse "
        "(any host, any state) returns AgentAlreadyExists "
        "(ADR-0005 §6.5)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Local handle for this agent (e.g. 'coder-1').",
            },
            "host": {
                "type": "string",
                "description": "Remote host (user@host or ssh alias).",
            },
            "project_dir": {
                "type": "string",
                "description": "Working directory on the remote host.",
            },
            "role_prompt": {
                "type": "string",
                "description": "System prompt establishing the agent's role.",
            },
            "additional_agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Peer agent names this agent accepts messages from. "
                    "One-way allowlist (ADR-0005 §6.2): only agents in "
                    "this list can TO: this one."
                ),
                "default": [],
            },
        },
        "required": ["name", "host", "project_dir", "role_prompt"],
        "additionalProperties": False,
    },
)


# Layout constants — kept module-private so tests can target them.
_ROLE_DIR = "/tmp/moderator"
_ROLE_FILENAME_TEMPLATE = "role-{name}.txt"
_TMUX_SESSION_TEMPLATE = "mod-{name}"
_AGENT_BIN = "claude"


def _remote_role_path(name: str) -> str:
    """Where the role prompt is staged on the agent host."""
    return f"{_ROLE_DIR}/{_ROLE_FILENAME_TEMPLATE.format(name=name)}"


def _tmux_session(name: str) -> str:
    return _TMUX_SESSION_TEMPLATE.format(name=name)


def _stage_role_locally(role_prompt: str) -> Path:
    """Write ``role_prompt`` to a host-local tempfile so the SSH driver
    can ``put_file`` it. Returns the temp ``Path``.

    The caller is responsible for cleaning up. We use ``mkstemp`` so
    the path is unique and the file is owned by us.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="moderator-role-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(role_prompt)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def _record_failure(
    *,
    name: str,
    host: str,
    project_dir: str,
    reason: str,
) -> None:
    """Best-effort: append a failure record. Never raises."""
    try:
        state = read_state()
    except Exception:
        from moderator.core.models import empty_state

        state = empty_state()

    state.agents[name] = AgentRecord(
        name=name,
        host=host,
        project_dir=project_dir,
        state=AgentState.ERROR,
        error=reason,
    )
    try:
        write_state(state)
    except Exception:
        pass


def _build_agent_command(remote_role_path: str, project_dir: str) -> str:
    """Build the tmux ``command`` string. Per ADR-0003 the agent
    process owns its role; we stage the role prompt to a file on
    the agent host and point the agent at it via ``--system-prompt-file``.

    ``--system-prompt-file`` replaces Claude Code's default system
    prompt with the file's contents — matches ADR-0003's "agent
    process owns its role" principle. (Earlier this code used the
    invented ``--role-file`` flag, which is not a real Claude Code
    CLI flag — see ticket 13 / bug B4. The agent exited rc=1
    immediately, tmux destroyed the session, and the next
    ``paste_buffer`` step failed with a misleading
    ``can't find pane: mod-...`` error.)
    """
    # Quoting: project_dir is treated as a shell path; the role
    # path is the file Claude Code will read.
    return f"cd {shlex_quote(project_dir)} && {_AGENT_BIN} --system-prompt-file {shlex_quote(remote_role_path)}"


def shlex_quote(s: str) -> str:
    """Tiny re-export so this module is self-contained."""
    import shlex

    return shlex.quote(s)


async def _start_one(
    *,
    name: str,
    host: str,
    project_dir: str,
    role_prompt: str,
    additional_agents: list[str],
    ssh: SshDriver,
    tmux: TmuxDriver,
) -> None:
    """Drive the real driver flow for a single agent.

    On success: writes a RUNNING record.
    On any failure: raises ``DriverError``; the caller records an
    error-state record on disk.
    """
    local_role = _stage_role_locally(role_prompt)
    try:
        remote_role = _remote_role_path(name)
        session = _tmux_session(name)

        # 0. Wire up the SSH driver to the per-agent host. The
        # runtime builds ParamikoSshDriver in lazy mode (no host
        # at construction), so the caller MUST invoke connect(host)
        # before the first network call. connect() is idempotent
        # (no-op when the driver is already connected), so calling
        # it here is safe even if a future change constructs the
        # driver eagerly. ticket 12 / bug B1.
        ssh.connect(host)

        # 1. Push the role prompt to the agent host. put_file is
        # required to create parent directories (LocalExecutor does;
        # real SSH drivers will too).
        ssh.put_file(local_role, remote_role)

        # 2. Create the tmux session. If it already exists, the
        # LocalExecutor raises DriverError; production tmux should
        # likewise refuse (ADR-0003 — explicit kill before restart).
        command = _build_agent_command(remote_role, project_dir)
        tmux.create_session(session, command)

        # 3. Pipe the role prompt in via paste_buffer. We do this
        # *after* create_session so the session is alive and ready.
        tmux.paste_buffer(session, role_prompt)

        # 4. Update state. STARTING was the default; flip to RUNNING.
        state = read_state()
        now = _utc_now_naive()
        state.agents[name] = AgentRecord(
            name=name,
            host=host,
            project_dir=project_dir,
            role_prompt_path=remote_role,
            tmux_session=session,
            state=AgentState.RUNNING,
            started_at=now,
            last_output_at=now,
            log_offset=0,
            additional_agents=list(additional_agents),
        )
        write_state(state)
    finally:
        # Clean up the local staging copy. Even if the put_file
        # succeeded, the remote already has it; the local copy is
        # only for transport.
        try:
            os.unlink(local_role)
        except OSError:
            pass


async def handle(arguments: dict[str, Any]) -> CallToolResult:
    """Validate inputs and drive the real driver flow."""
    name = arguments.get("name")
    host = arguments.get("host")
    project_dir = arguments.get("project_dir")
    role_prompt = arguments.get("role_prompt")
    additional_agents = arguments.get("additional_agents", []) or []

    if not (name and host and project_dir and role_prompt):
        missing = [
            k
            for k in ("name", "host", "project_dir", "role_prompt")
            if not arguments.get(k)
        ]
        return make_error(f"start_session: missing required args: {missing}")

    if not isinstance(additional_agents, list) or not all(
        isinstance(a, str) for a in additional_agents
    ):
        return make_error(
            "start_session: 'additional_agents' must be a list of agent names"
        )

    # Defensive: name must be a safe shell identifier so it can be
    # embedded in tmux session names and remote paths. ADR-0008
    # requires this and ticket 05 extends it for peer routing.
    if not _is_safe_name(name):
        reason = (
            f"start_session({name!r}): name must match "
            f"^[A-Za-z0-9_-]+$ (got {name!r})"
        )
        _record_failure(
            name=name,
            host=host,
            project_dir=project_dir,
            reason=reason,
        )
        return make_error(reason)

    # ADR-0005 §6.5: same-name reject. Check BEFORE acquiring drivers
    # so we don't side-effect (tmux session creation, file put)
    # when the call is going to fail anyway.
    existing = read_state().agents.get(name)
    if existing is not None:
        return make_error(
            f"AgentAlreadyExists(name={name!r}, host={existing.host!r}, "
            f"state={existing.state.value!r})"
        )

    try:
        ssh, tmux = get_drivers()
    except DriverMissing as exc:
        reason = (
            f"start_session({name!r}): {exc}. "
            f"Set MODERATOR_DRIVER=local for development."
        )
        _record_failure(
            name=name, host=host, project_dir=project_dir, reason=reason
        )
        return make_error(reason)

    try:
        await _start_one(
            name=name,
            host=host,
            project_dir=project_dir,
            role_prompt=role_prompt,
            additional_agents=additional_agents,
            ssh=ssh,
            tmux=tmux,
        )
    except DriverError as exc:
        reason = (
            f"start_session({name!r}, {host!r}) failed: {exc}"
        )
        _record_failure(
            name=name, host=host, project_dir=project_dir, reason=reason
        )
        return make_error(reason)
    except Exception as exc:  # last-resort: never propagate to caller
        reason = (
            f"start_session({name!r}, {host!r}) failed: "
            f"unexpected {type(exc).__name__}: {exc}"
        )
        _record_failure(
            name=name, host=host, project_dir=project_dir, reason=reason
        )
        return make_error(reason)

    return make_text_result(
        f"start_session: {name!r} on {host!r} → running "
        f"(session={_tmux_session(name)!r})"
    )


def _is_safe_name(name: str) -> bool:
    """Return True iff ``name`` is a portable identifier — letters,
    digits, underscore, dash. Used to keep names shell-safe (tmux
    sessions, remote paths)."""
    if not name:
        return False
    return all(c.isalnum() or c in "_-" for c in name)


# Module-level export of the safe-name predicate so tests can
# import it directly without reaching through ``_is_safe_name``.
is_safe_agent_name = _is_safe_name


__all__ = ["TOOL", "handle", "is_safe_agent_name"]