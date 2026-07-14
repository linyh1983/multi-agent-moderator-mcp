# 06 — Stuck / offline detection + no auto-restart

**What to build:** the supervisor detects an Agent that has gone silent (`stuck`) or whose tmux session is gone (`offline`), transitions state accordingly, and does **not** auto-restart. Recovery requires moderator action.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] When `now - last_output_at > stuck_threshold_minutes` (default 30 min) AND the tmux session is still alive, the agent transitions `running → stuck`
- [ ] `last_output_at` updates on **any** byte read from stdout, not just markers (a noisy `find /` still counts as activity)
- [ ] When `is_alive()` returns False twice in a row (one failed poll + one retry, separated by current poll interval), the agent transitions `running → offline`
- [ ] In `offline` state, the worker keeps polling `is_alive()` at the regular interval — if the tmux session reappears and new bytes arrive, the agent transitions back to `running` (silent self-recovery is observable to the moderator only via `check`)
- [ ] **No automatic restart**: the worker never spawns a new tmux session on the agent's behalf under any condition
- [ ] Moderator can manually recover with `stop_session(name)` followed by `start_session(name, ...)` — even from `offline`
- [ ] `error` state is **never** self-healing; only moderator `stop_session` clears it (to `stopped`)
- [ ] `state.agents[name].last_error` records the last `is_alive()` failure reason
- [ ] User stories covered: A.30, A.31, D.64, D.73
- [ ] ADRs respected: ADR-0008 (stuck / offline / error semantics), ADR-0017 (no auto-restart)