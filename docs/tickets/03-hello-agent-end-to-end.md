# 03 — Hello Agent end-to-end (check sees it)

**What to build:** `check()` renders a global view: urgent help / pending approvals / agents (state + last-output age + progress count + tail) / recent chat. Full 7-state lifecycle (`starting / running / blocked / stuck / offline / stopped / error`) is observable.

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] `check()` output is grouped in this priority order: `## Urgent help` → `## Pending approvals` → `## Agents` → `## Recent chat (tail)`
- [ ] Each agent row shows: name, host, state, last-output age, progress entry count + last 1–3 lines (no full bodies)
- [ ] Agents in `stuck` / `offline` / `error` are visually flagged so they stand out
- [ ] When the state file's `mtime` differs from the last-seen `mtime` in this process, `check()` prepends an external-change warning (e.g. `⚠️  State changed externally since you last read it…`)
- [ ] `stop_session(name, grace_seconds=30)` sends a graceful signal and (after grace) kills the tmux session; the agent record transitions to `stopped` and is retained on disk with full postmortem fields
- [ ] `stop_session(name, force=True)` kills the tmux session immediately
- [ ] When an agent is stopped, its pending actions (if any) auto-cancel with `cancelled_reason="agent_stopped"`
- [ ] All 7 AgentState values are exercised in tests; illegal transitions raise errors
- [ ] User stories covered: A.9, A.10, A.11, A.12, A.13, A.27, A.28, A.29, A.30, A.31, B.46, B.47, B.48, D.65
- [ ] ADRs respected: ADR-0008 (lifecycle), ADR-0009 §4.4 (stop → auto-cancel), ADR-0011 (mtime warning), ADR-0014 (AgentRecord fields)