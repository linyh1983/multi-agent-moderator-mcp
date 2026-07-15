# 11 — LocalTmuxDriver (optional) — enable manual marker round-trip tests

**What to build:** a second `LocalExecutor`-family driver whose `TmuxDriver`
half uses the **system `tmux` binary** instead of an in-process subprocess.
This lets manual integration tests exercise marker round-trip via
`tmux send-keys -t mod-<name> 'echo 【进度汇报】...【/进度汇报】' Enter`
the same way an operator would on a remote host.

**Status:** optional — only do this if manual integration testing is
prioritized over automation coverage. The 216 pytest cases already cover
marker parser, worker dedup, rate limits, and writeback caps via the
existing in-process `LocalExecutor`.

**Why this matters:** `LocalExecutor` (ticket 02) deliberately substitutes
the user's command with a static `_ECHO_AGENT` subprocess whose stdin
only the moderator's internal `send_keys` can write to. External tools
(Git Bash, `tmux send-keys`) have no path to that pipe. Manual marker
testing therefore cannot be done under the current LocalExecutor.

A real-`tmux`-backed local driver bridges the gap between in-process
mocking and full remote SSH, at the cost of requiring tmux to be
installed locally.

**Blocked by:** 02 (driver Protocol definitions)

## Scope

### LocalTmuxDriver

A new `TmuxDriver` implementation that calls the system `tmux` binary
via `subprocess.run`:

| Protocol method | tmux invocation |
|---|---|
| `create_session(name, cmd)` | `tmux new-session -d -s <name> '<cmd>'` |
| `capture_pane(name, lines)` | `tmux capture-pane -t <name> -p -S -<lines>` |
| `send_keys(name, text)` | `tmux send-keys -t <name> '<text>'` |
| `paste_buffer(name, text)` | `tmux load-buffer -` + `tmux paste-buffer -t <name>` (stdin=buffer text) |
| `is_alive(name)` | `tmux has-session -t <name>` (rc=0 iff alive) |
| `kill_session(name)` | `tmux kill-session -t <name>` |

Each invocation goes through `subprocess.run(["tmux", ...])` with no shell
to avoid injection; the binary resolves via `shutil.which("tmux")` and
raises `DriverError` if not on PATH.

### Pairing with LocalSshDriver (no-op)

`LocalTmuxDriver` only implements the `TmuxDriver` half. The `SshDriver`
half is satisfied by a thin `LocalSshDriver` that does nothing (all
file ops are already host-local). This is the same composition pattern
as `RemoteTmuxDriver` in ticket 09, just without the SSH hop.

### Runtime selection

Add a new `MODERATOR_DRIVER=local-tmux` value (distinct from `local`
and `ssh`) that returns `(LocalSshDriver(), LocalTmuxDriver())`.

## TDD Path

1. **Protocol test:** `LocalTmuxDriver` satisfies the `TmuxDriver`
   Protocol (runtime-checkable). Pure structural test, no subprocess.
2. **Binary-resolved test:** `LocalTmuxDriver.__init__` raises
   `DriverError` if `tmux` is not on PATH (simulate by patching
   `shutil.which`).
3. **Subprocess contract test:** mock `subprocess.run`; verify each
   method issues the expected `tmux` arguments and returns the parsed
   result. This is the same shape as the contract test for
   `ParamikoSshDriver` in ticket 09.
4. **Real-tmux integration test:** on the user's Git Bash environment
   (which has tmux installed per `tmux -V`), exercise create → send →
   capture → kill against a test session. Cleanup in fixture teardown.
5. **End-to-end manual test:** with `.mcp.json` set to
   `MODERATOR_DRIVER=local-tmux`, restart Claude Code, run:
   ```
   moderator start_session marker-test (localhost)
   tmux send-keys -t mod-marker-test 'echo 【进度汇报】ping【/进度汇报】' Enter
   moderator check
   ```
   Expect `progress` to contain `ping`. This is the test that motivated
   this ticket.

## Acceptance Checklist

- [ ] `LocalTmuxDriver` class in `src/moderator/drivers/local_tmux.py`
- [ ] Satisfies `TmuxDriver` Protocol (runtime-checkable)
- [ ] `__init__` raises `DriverError` if `tmux` not on PATH
- [ ] All 6 Protocol methods implemented via `subprocess.run(["tmux", ...])`
- [ ] No shell=True (avoids command injection from role prompts)
- [ ] `LocalSshDriver` no-op in `src/moderator/drivers/local_ssh.py`
- [ ] `MODERATOR_DRIVER=local-tmux` selects the pair in `runtime.get_drivers()`
- [ ] Manual marker round-trip test passes (verified by running the
   shell sequence above)
- [ ] No regression on existing 216+ pytest cases
- [ ] mypy clean on all source modules

## Out of Scope

- Cross-platform tmux quirks — assume POSIX (Linux / macOS / WSL).
  Windows native tmux (via Cygwin or MSYS2) is best-effort
- Tmux server lifecycle — let the default server handle it
- Session naming conflicts — `tmux new-session -d` errors out naturally;
  surface as `DriverError`

## ADRs Respected

- **ADR-0003** (SSH + tmux) — uses real tmux, the canonical transport
- **ADR-0014** (state schema) — no schema changes
- **ADR-0017** (no auto-restart) — driver does not spawn unsolicited

## Reference

- Discovered during manual integration testing 2026-07-15
  (see `MANUAL_TEST_REPORT.md` §"Tests Blocked by Architecture" §6)
- Sister ticket to #9 (ParamikoSshDriver) — both bridge the gap
  between LocalExecutor and real remote execution, just at different
  points on the spectrum