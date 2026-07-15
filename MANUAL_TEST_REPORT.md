# Manual Integration Test Report

**Date**: 2026-07-15
**Branch**: main
**State**: 7 implementation tickets (02-08) closed; 216 pytest passing; mypy clean on 26 modules
**Tester**: Clark Lin (Git Bash on Windows 11)
**Environment**: LocalExecutor mode (`MODERATOR_DRIVER=local`), real Claude Code MCP client

## TL;DR

Manual integration tests reveal **two material gaps between ticket descriptions
and shipped behavior**:

1. **`ParamikoSshDriver` is not implemented** despite ticket 02's commit
   message claiming "SSH/tmux driver". Only `LocalExecutor` was delivered;
   the real SSH driver was left for a future ticket and never created.
2. **`cue` MCP tool is a stub** despite being part of the 6-tool registry.
   Tickets 02/04 delivered `approve` and `reject`, but `cue` was
   inadvertently left at its ticket-01 placeholder.

Beyond these two gaps, **the LocalExecutor-based flows work end-to-end**:
`start_session`, `check`, `stop_session`, lifecycle transitions, and the
same-name reject invariant (ADR-0005 §6.5) all behave correctly when driven
through the real MCP stdio protocol.

## Environment Setup

| Step | Result |
|---|---|
| `python -m venv .venv` + `source .venv/Scripts/activate` | OK (Git Bash) |
| `pip install -e ".[dev]"` | OK; `multi-agent-moderator-mcp 0.1.0` registered |
| `pip install "mcp>=1.0"` | Required — `pyproject.toml` declares the dep but didn't install in a fresh venv; once added, `mypy src` was clean |
| `.mcp.json` with Windows paths (`E:/...`) | OK after switching from MSYS paths (`/e/...`) which Claude Code doesn't translate |
| Full Claude Code restart (kill process, not new session) | Required for `.mcp.json` to be re-read |

## Tests Executed

### 1. start_session happy path — PASS

```
moderator start_session:
  name: local-coder-1
  host: localhost
  project_dir: E:/projects/multi-agent-moderator-mcp
  role_prompt: "..."
```

Result: `running (session='mod-local-coder-1')`. State file shows agent
record with `state=running`, `tmux_session='mod-local-coder-1'`.

### 2. Same-name reject (ADR-0005 §6.5) — PASS

```
moderator start_session:
  name: marker-test   # already exists in state
  ...
```

Result: `AgentAlreadyExists(name='marker-test', host='localhost',
state='running')`. The check happens BEFORE driver acquisition, so no
side effects (verified by ticket 02 contract).

### 3. stop_session — PASS

```
moderator stop_session marker-test
```

Result: state transitioned to `stopped`, `grace_seconds=30`, `force=False`,
0 pending actions to cancel. Records retained on disk (ADR-0008 §2.6).

### 4. check() rendering — PASS

`check` returns human-readable agent state including running/stopped,
last_output age, progress count, chat, and warnings about external
mtime changes (a useful accidental feature).

### 5. Worker polling — PASS (indirect)

`last_output_at` advances on every `check`, indicating the worker poll
loop is active at the configured `poll_interval_seconds=2`.

## Tests Blocked by Architecture

### 6. Progress marker round-trip — BLOCKED

LocalExecutor (`src/moderator/drivers/local.py:140-158`) deliberately does
not run the user-supplied `command`; it spawns a static `_ECHO_AGENT`
Python subprocess and only the moderator's internal `send_keys` can
inject bytes into its stdin. External tools (Git Bash, `tmux send-keys`)
have no path to that pipe. Marker round-trip therefore cannot be
exercised manually under LocalExecutor.

The 216 pytest cases covering marker parser, worker dedup, rate limits,
and writeback caps (ticket 02 + 05 + 07) remain the source of truth for
this layer.

### 7. approve_action / reject_action — BLOCKED

Tools are implemented (161 + 172 lines respectively, ticket 04 commit
`d2825a6`) but their input is `pending` ApprovalAction records, which
only the worker creates from `【申请执行】` markers — see test 6.

### 8. cue — NOT IMPLEMENTED

`src/moderator/tools/cue.py` is 41 lines and explicitly returns
`cue: not implemented in ticket 01 (lands in ticket 04)`. The ticket-04
commit shipped `approve` + `reject` but not `cue`. The description in
the `Tool` definition still points to ticket 04, which has already
landed — so the routing comment is misleading.

### 9. Real SSH driver — NOT IMPLEMENTED

`start_session` with `host=django-app-openeuler-service-10` returns:
```
ParamikoSshDriver is not yet implemented; ticket 02 covers the local executor.
Set MODERATOR_DRIVER=local for development, or install paramiko and implement
this driver before production deployment.
```

The ticket-02 commit message (`420ef18`) advertises "real SSH/tmux
driver + progress marker + worker poll" but in fact only delivered
the `LocalExecutor` plus `SshDriver`/`TmuxDriver` Protocol definitions.
No real SSH driver class exists.

Verified manually that the user's actual SSH+tmux+Claude Code stack
works (hand-tested via `ssh django-app-openeuler-service-10 "tmux ..."`),
so the platform is ready when the driver lands.

## Gap Analysis: Tickets vs Shipped Behavior

| Ticket | Commit | Description Claim | Actually Shipped | Gap |
|---|---|---|---|---|
| 01 | 95b5b0a | bootstrap with state, CLI, 6 tool stubs | All 6 stubs registered; `start_session` and `check` had partial semantics | None — description accurate |
| 02 | 420ef18 | "real SSH/tmux driver + progress marker + worker poll" | `LocalExecutor` (no SSH), marker parser, worker poll | **SSH driver missing** — commit message overstates scope |
| 03 | 3a8b842 | `check()` rendering + `stop_session` + 7-state lifecycle | All three delivered | None |
| 04 | d2825a6 | approval roundtrip + writeback + invariants | `approve` + `reject` delivered; `cue` left as stub | **`cue` not delivered** despite being part of the 6-tool plan |
| 05 | 11d24dc | peer routing allowlist + same-name reject + parse warnings | All delivered | None |
| 06 | 83129ae | stuck/offline detection + no auto-restart | All delivered | None |
| 07 | c195b94 | dedup FIFO 5000 + per-agent rate limit + 64 KiB writebacks | All delivered | None |
| 08 | 9a0d052 | schema_version refusal + migration framework + inspect hint | All delivered | None |

## Recommended Follow-Up Tickets

### Ticket 09: ParamikoSshDriver

Implements the missing `SshDriver` Protocol against `paramiko.SSHClient`.
Must satisfy: `connect`, `exec`, `put_file`, `is_alive`. `TmuxDriver`
calls then run over `exec("tmux new-session -d -s ...")`, `exec("tmux
capture-pane -t ...")`, etc. The protocol seam designed in ticket 02
keeps this work contained to one file plus its tests.

TDD path:
1. Contract test using a fake `paramiko.SSHClient` mock
2. Integration test against `ssh django-app-openeuler-service-10` (the
   user's actual dev host) to verify auth, exec, and `put_file` over a
   real socket
3. Wire `MODERATOR_DRIVER=ssh` to select the new driver in
   `moderator.runtime.get_drivers()`

### Ticket 10: cue tool

Land the actual `cue` MCP tool implementation. The schema is already
defined in `src/moderator/tools/cue.py`; the missing piece is the
handler that calls `TmuxDriver.send_keys` to inject `<moderator-cue>`
text into the target agent's tmux session, plus the state-side tracking
of in-flight cues for ADR-0013 §5.4 rate limiting.

TDD path:
1. Test: `cue(target=name, message=...)` writes to tmux send_keys buffer
2. Test: `cue(target=all)` fans out to every running agent respecting
   the per-agent rate limit
3. Test: `cue` against `target='moderator'` returns an error
   (ADR-0005: TO:moderator forbidden — applies symmetrically)

### Optional Ticket 11: LocalTmuxDriver

A second local driver that uses the system `tmux` binary instead of an
in-process subprocess. Lets manual tests exercise marker round-trip via
`tmux send-keys -t mod-<name> 'echo 【进度汇报】...【/进度汇报】' Enter`.
This bridges the gap between LocalExecutor (closed-loop) and real SSH
(open-loop) without requiring a remote host.

## Conclusion

The shipped code is **internally consistent** — 216 tests pass, mypy is
clean, the 4 fully-implemented tools behave correctly under real MCP
stdio, and all tested ADRs hold under manual integration. The two
unfilled tickets (09 and 10) are **scope misrepresentation in commit
messages**, not regressions — the work simply was never done despite
appearing to be.

MVP business logic is solid; the path to production requires tickets
09 and 10.