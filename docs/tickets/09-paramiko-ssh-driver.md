# 09 — ParamikoSshDriver (real remote SSH)

**What to build:** the missing `SshDriver` implementation against
`paramiko.SSHClient`. Today only `LocalExecutor` exists; `MODERATOR_DRIVER=ssh`
returns `ParamikoSshDriver is not yet implemented`. This ticket closes
that gap so moderators can run agents on real remote hosts.

**Why this matters:** ticket 02's commit message claimed "real SSH/tmux
driver + progress marker + worker poll" but only delivered `LocalExecutor`.
This is the largest discrepancy between shipped behavior and commit
description; until it lands, the moderator is effectively local-only.

**Blocked by:** 02 (driver Protocol definitions already in place)

**Status:** ready-for-agent

## Scope

### SshDriver (paramiko-backed)

Implements the existing `SshDriver` Protocol:

```python
class SshDriver(Protocol):
    def connect(self, host: str) -> None: ...
    def exec(self, cmd: str, *, timeout: float | None = None) -> tuple[int, str, str]: ...
    def put_file(self, local: Path, remote: str) -> None: ...
    def is_alive(self) -> bool: ...
    def close(self) -> None: ...
```

Implementation choices:

- Use `paramiko.SSHClient` with `paramiko.AutoAddPolicy` for known-host
  handling (matches the user's verified dev host: `django-app-openeuler-service-10`)
- Use `paramiko.SFTPClient` for `put_file`; create parent directories via
  `sftp.mkdir(...)` walking the path
- Auth: prefer key-based auth (read from `~/.ssh/` automatically); fall
  back to agent forwarding; never store passwords
- Connection pooling: one `SSHClient` per `host` per `LocalExecutor`
  instance, reused across `exec` calls; `close()` on shutdown

### TmuxDriver over SSH

The existing `TmuxDriver` Protocol (already implemented by `LocalExecutor`)
runs against the agent host via `SshDriver.exec`:

- `create_session`: `ssh exec("tmux new-session -d -s <name> '<command>'")`
- `capture_pane`: `ssh exec("tmux capture-pane -t <name> -p -S -<lines>")`
- `send_keys`: `ssh exec("tmux send-keys -t <name> '<text>'")`
- `paste_buffer`: `ssh exec("tmux load-buffer -; tmux paste-buffer -t <name>")`
  with the buffer text piped via stdin
- `is_alive`: `ssh exec("tmux has-session -t <name>")` returns rc=0 iff alive

The TmuxDriver layer is shared with LocalExecutor — same Protocol, two
backends. No changes needed to existing TmuxDriver code; only the SSH
backend is new.

### Runtime wiring

`moderator.runtime.get_drivers()` selects drivers based on `MODERATOR_DRIVER`:

| Env value | SshDriver | TmuxDriver |
|---|---|---|
| `local` (default) | `LocalExecutor` (ssh half) | `LocalExecutor` (tmux half) |
| `ssh` | `ParamikoSshDriver` | `RemoteTmuxDriver(exec_via=ssh_driver)` |

## TDD Path

1. **Contract test (no I/O):** mock `paramiko.SSHClient` via
   `unittest.mock.MagicMock`. Verify the driver satisfies the `SshDriver`
   Protocol: each method makes the expected paramiko calls and propagates
   exceptions as `DriverError`.

2. **Auth integration test:** run against the verified dev host
   `django-app-openeuler-service-10`. Verify `connect` succeeds using
   the existing key (the user already hand-tests this every session).
   Verify `exec("echo OK")` returns `(0, "OK\n", "")`.

3. **put_file integration test:** SFTP a small text file to
   `/tmp/moderator/test.txt`; verify file exists with correct content
   via a follow-up `exec("cat /tmp/moderator/test.txt")`.

4. **Tmux-over-SSH test:** create a session, send keys, capture pane,
   kill session — all via `SshDriver.exec`. Assert state matches what
   `LocalExecutor` produces for the same operations.

5. **End-to-end via MCP:** register `MODERATOR_DRIVER=ssh` in `.mcp.json`,
   restart Claude Code, run the manual test path from
   `MANUAL_TEST_REPORT.md`. `start_session` should now succeed against
   `django-app-openeuler-service-10` and produce a real tmux session
   the user can inspect with `tmux list-sessions`.

## Acceptance Checklist

- [ ] `ParamikoSshDriver` class in `src/moderator/drivers/ssh.py`
- [ ] Satisfies `SshDriver` Protocol (runtime-checkable)
- [ ] `connect` raises `DriverError` on auth failure with a clear message
- [ ] `exec` captures stdout/stderr and exit code; respects `timeout`
- [ ] `put_file` creates parent directories on the remote host
- [ ] `is_alive` checks the underlying transport without sending a command
- [ ] `close` is idempotent and safe to call multiple times
- [ ] `RemoteTmuxDriver` in `src/moderator/drivers/tmux_remote.py`
  delegates every method to `SshDriver.exec`
- [ ] `MODERATOR_DRIVER=ssh` selects the new drivers via `runtime.get_drivers()`
- [ ] User's `django-app-openeuler-service-10` host is the documented
  integration test target (verified manually each session)
- [ ] No regression on existing 216 pytest cases
- [ ] mypy clean on all source modules including new ones
- [ ] Add `paramiko` to `[project.dependencies]` in `pyproject.toml`

## Out of Scope

- Connection multiplexing / control master — single SSHClient per host
  is sufficient for v1
- Agent forwarding chains — flat topology only
- Windows-side SSH agent integration — user is on Git Bash; the paramiko
  default `~/.ssh/` lookup is sufficient

## ADRs Respected

- **ADR-0003** (SSH + tmux) — the protocol described here is what 0003
  always envisioned; this ticket finally lands it
- **ADR-0014** (state schema) — no schema changes needed; this is pure
  transport
- **ADR-0017** (no auto-restart) — driver does not spawn sessions
  unsolicited

## Reference

See `MANUAL_TEST_REPORT.md` §"Tests Blocked by Architecture" and
§"Recommended Follow-Up Tickets" for the discovery context.