# 04 — Approval roundtrip (申请 → approve → writeback)

**What to build:** an Agent can send `【申请执行】`, the moderator can `approve`/`reject`, and the Agent receives the corresponding `<moderator-…>` tag. ApprovalAction is a permanent log; single-wait invariant holds; batch is all-or-nothing.

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] Agent sends `【申请执行】X【/申请执行】` → a new `ApprovalAction` is created in `state.actions` with `id="a-<seq>"`, `status="pending"`, `created_at`, and `agent_name`
- [ ] Server immediately sends `<moderator-info action-id="a-N" status="queued">` writeback to the Agent as acknowledgement
- [ ] Agent state transitions `running → blocked` while its action is pending; only an approve/reject for that exact action_id unblocks it
- [ ] A `cue` directed at the blocked Agent does NOT unblock it (single-wait invariant)
- [ ] `approve(action_ids=[...], note?)` validates every id is `pending` first; if any id is missing or non-pending, the whole call errors and **no state changes**
- [ ] On success, all listed actions transition to `approved`; for each, a `<moderator-approve action-id="..." note="...">` tag is written back to the corresponding Agent
- [ ] `reject(action_ids=[...], reason)` behaves the same way (all-or-nothing) but transitions to `rejected` and writes `<moderator-reject action-id="..." reason="...">`
- [ ] `reject` does NOT auto-create any new pending action — Agent's submission log stays 1:1
- [ ] `reject` reason is stored verbatim in `state.actions[id].reject_reason`; in `state.chat`, peer agents see the entry marked `private_to_originator` and never the verbatim text
- [ ] When an Agent is `stopped`, its pending actions are auto-cancelled with `cancelled_reason="agent_stopped"` (already in #03 — verify here)
- [ ] User stories covered: A.15, A.16, A.17, A.18, A.19, A.20, A.21, A.22, B.36, B.37, B.38, B.40, B.41
- [ ] ADRs respected: ADR-0007 (writeback + ack), ADR-0009 (single-wait / all-or-nothing / privacy tiers)