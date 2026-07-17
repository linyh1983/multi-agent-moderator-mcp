# 13 — Agent CLI flag rename: `--role-file` → `--system-prompt-file` (ticket 09 follow-up)

**What to build:** one-line fix for a string-level bug surfaced
during the manual R2-S1 retest of ticket 12 on 2026-07-16.

The full bug report lives in `tmp_manual/TEST_PLAN.md` §"Bug
log (retest)" → **B4**. After B1+B2+B3 landed and the SSH wiring
chain worked end-to-end, the next failure was:

```
RemoteTmuxDriver.paste_buffer('mod-coder-e2e'): load/paste exited 1:
can't find pane: mod-coder-e2e
```

— misleading because the actual root cause is upstream: the agent
process itself exited `rc=1` immediately after `tmux.create_session`,
and tmux's default `remain-on-exit off` destroyed the session
before the next step ran.

**Why this matters:** ticket 09's commit message claims end-to-end
SSH/tmux works, but the agent CLI flag was invented for the
moderator's interface contract (`--role-file`) and is not a real
Claude Code flag. `claude --help` on the dev host confirms:
`--system-prompt-file`, `--system-prompt`, `--append-system-prompt`,
`--append-system-prompt-file`, `--add-dir`, `--agent`, `--bare`,
… — but **no `--role-file`**. So every `start_session` against any
host with Claude Code installed crashes the agent immediately.

**Blocked by:** 09 (driver class exists), 12 (B1+B2+B3 wiring
verified).

**References:** issues/9 (ticket 09), tmp_manual/TEST_PLAN.md §B4.

**Status:** fix staged in working tree (2026-07-17); awaiting manual R2-S1 retest; NOT yet committed per `MEMORY.md` policy

## Scope

### Fix A — `_build_agent_command` flag rename

In `src/moderator/tools/start_session.py:_build_agent_command`
(line 163 area), change:

```python
# Before (BUG):
return f"cd {shlex_quote(project_dir)} && {_AGENT_BIN} --role-file {shlex_quote(remote_role_path)}"

# After (FIX):
return f"cd {shlex_quote(project_dir)} && {_AGENT_BIN} --system-prompt-file {shlex_quote(remote_role_path)}"
```

`--system-prompt-file` replaces the default Claude Code system
prompt with the file's contents. This matches ADR-0003's "agent
process owns its role" principle: the moderator stages the role
file to `/tmp/moderator/role-<name>.txt` on the agent host, then
points the agent at it via this flag. The agent reads it on
startup as its sole system prompt.

The alternative `--append-system-prompt-file` would have preserved
Claude Code's built-in default system prompt and appended the role
file as supplemental context. The user (Clark) chose
`--system-prompt-file` for v1 simplicity; we can revisit if users
want the default Claude Code behavior preserved.

No other code changes required — the assembled string is the only
surface this breaks.

### Fix B — Regression tests

**New file**: `tests/tools/test_start_session_agent_command.py`.
Two tests (plus companion assertions) pin the contract:

1. **`test_build_agent_command_uses_system_prompt_file_flag`** (pure unit):
   imports `_build_agent_command` directly, asserts the returned
   string contains `--system-prompt-file` and **NOT** `--role-file`.
2. **`test_start_session_invokes_tmux_create_with_system_prompt_file_command`**
   (integration-style): drives `call_tool("start_session", ...)`
   against an `_RecordingTmux` that captures the `command` argument
   passed to `tmux.create_session`. Asserts the captured command
   uses `--system-prompt-file` end-to-end.

Companion assertions (`test_build_agent_command_includes_role_path`,
`test_build_agent_command_quotes_project_dir`) lock in the role
path and `cd` prefix are preserved.

### Out of scope

- **Fix C (clearer error when agent CLI fails fast)** — TEST_PLAN.md
  flagged this as optional / not required. The B4 fix alone makes
  R2-S1 work end-to-end. If a future agent CLI change breaks
  startup again, the misleading `can't find pane` error from
  `paste_buffer` will recur — at that point, add a
  `sleep ~100ms; check tmux.is_alive; raise DriverError("agent
  command died: ...")` guard in `_start_one` immediately after
  `tmux.create_session`.

- `--append-system-prompt-file` alternative — deferred per Clark's
  call. The fix is one line if we ever want to switch.

## Acceptance

- `tests/tools/test_start_session_agent_command.py::*` (4 tests) pass
- All existing 286+ tests still green (2 known failures in
  `test_local.py` are pre-existing and out of scope)
- `mypy` clean on the touched module
- Manual `R2-S1` from `tmp_manual/TEST_PLAN.md` returns `isError=false`
  with text `→ running (session='mod-coder-e2e')` against the real
  dev host `django-app-openeuler-service-10`
- Manual `R2-S6` from `tmp_manual/TEST_PLAN.md` shows the
  `<moderator-cue>...</moderator-cue>` wrapper arriving literally
  on the remote pane (B5 verification — see tmp_manual/TEST_PLAN.md
  §"B5")

## Verification (this commit)

- `pytest` → 290 passed (2 known pre-existing failures out of scope)
- `mypy src/moderator/` → clean
- Working tree staged; **NOT yet committed** per `MEMORY.md` policy
  "修 bug 后等用户测再 commit" — awaiting Clark's manual R2-S1 retest.