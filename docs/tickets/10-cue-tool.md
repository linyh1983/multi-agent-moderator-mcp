# 10 — cue tool (real implementation)

**What to build:** the real `cue` MCP tool implementation. The stub is
registered in the 6-tool registry, and `approve` / `reject` from ticket
04 work, but `cue` was never delivered — its handler is still
`return make_error("cue: not implemented in ticket 01 (lands in ticket 04)")`.

**Why this matters:** `cue` is how moderators send directives to running
agents (`cue(target=name, message=...)` writes the message into the
agent's tmux session). Without it, the moderator cannot steer agents
except by stopping and restarting them.

**Blocked by:** 03 (worker / tmux session lifecycle); 04 (cue is part
of the approval / cue messaging cluster described there)

**Status:** ready-for-agent

## Scope

### Tool signature

Already defined in `src/moderator/tools/cue.py`:

```python
TOOL = Tool(
    name="cue",
    inputSchema={
        "type": "object",
        "properties": {
            "target": {"type": "string"},   # agent name, "all", or role label
            "message": {"type": "string"},  # directive body
        },
        "required": ["target", "message"],
    },
)
```

### Handler behavior

1. Validate `target` and `message` are non-empty strings
2. Resolve `target`:
   - If `target == "moderator"`: return `make_error("cue to 'moderator' forbidden")` (ADR-0005 symmetry)
   - If `target == "all"`: resolve to all agents in `state.agents` whose `state == RUNNING`
   - If `target` is a role label: TBD — for v1, only single-agent and `"all"` are supported; reject role labels as `make_error("role-label cues not yet supported")`
   - Else: look up `state.agents[target]`; if missing, error
3. For each resolved target:
   - Verify `state == RUNNING` (or `BLOCKED` — cue can be sent but won't unblock per ADR-0009 §4.1); error otherwise
   - Wrap message as `<moderator-cue>{message}</moderator-cue>`
   - Call `tmux.send_keys(session, wrapped)` via the active driver
   - Record the cue as a `ChatMessage { kind="cue", from_agent=None, to_agent=target, text=message, ts=now }`
4. Honor per-agent rate limit (ADR-0013 §5.4): if `cue` would push any
   single agent's marker count past 10/sec, sleep before sending. For
   the cue message itself we don't count it as a marker (cue is
   moderator→agent, not agent→moderator); this is a sender-side throttle
   to avoid flooding tmux buffers.
5. Return text result: `cue: sent to <N> agent(s)`

### Tool description update

The current description says "Stub: not implemented in ticket 01 (lands
in ticket 04)". Replace with a real description of what `cue` does.

## TDD Path

1. **Validation tests:** empty target, empty message, both error
2. **Resolution tests:**
   - `target="moderator"` → forbidden error
   - `target="nonexistent"` → `AgentNotFound` error
   - `target="all"` → fans out to all RUNNING agents
   - agent in `STOPPED` / `ERROR` / `OFFLINE` → rejected with state in message
3. **Send behavior test (LocalExecutor):**
   - `cue(target=A, message="hi")` writes `<moderator-cue>hi</moderator-cue>\n`
     into A's tmux session stdin
   - `state.chat` gains a `ChatMessage { kind="cue", to_agent="A", text="hi" }`
4. **Rate limit test:** 11 cues to one agent within 1 second cause the
   11th to be delayed by ≥ 100ms (ADR-0013 §5.4)
5. **No-unblock test:** cue to a BLOCKED agent does NOT clear the
   blocking action (ADR-0009 §4.1 — single-wait invariant)
6. **Round-trip via MCP:** restart Claude Code, run `moderator cue
   <name>: please wrap up`, verify tmux capture-pane shows the message

## Acceptance Checklist

- [ ] `cue` handler in `src/moderator/tools/cue.py` no longer returns
  the "not implemented" error
- [ ] Tool description reflects real behavior
- [ ] `target` validation: rejects empty, rejects "moderator",
  resolves "all", looks up agent names
- [ ] Wraps message in `<moderator-cue>...</moderator-cue>` before
  sending to tmux
- [ ] Records a `ChatMessage` per delivery
- [ ] Honors per-target rate limit (no global flood)
- [ ] Does NOT unblock BLOCKED agents
- [ ] No regression on existing 216+ pytest cases
- [ ] mypy clean on all source modules

## Out of Scope

- Role-label cues (e.g. `cue(target="reviewer", ...)`) — return
  `make_error("role-label cues not yet supported")` for now
- Cue history / replay — just append to `state.chat`
- Conditional cues (cue when X happens) — moderator triggers manually
- Cue templates / canned messages — moderator composes the message

## ADRs Respected

- **ADR-0005** (TO:moderator forbidden) — symmetric: cue to "moderator" is forbidden
- **ADR-0009** §4.1 (single-wait) — cue to a BLOCKED agent does not unblock
- **ADR-0013** §5.4 (per-agent rate) — sender-side throttle

## Reference

See `MANUAL_TEST_REPORT.md` §"Tests Blocked by Architecture" — `cue`
was identified as a stub during manual integration testing on 2026-07-15.