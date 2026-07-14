# 05 — Peer messaging + routing allowlist + 同名拒绝

**What to build:** two Agents can exchange `【TO:<name>】` messages according to the `additional_agents` allowlist; misrouted or undeliverable messages get explicit acks; same-name Agent creation is rejected.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] `start_session(name="A", additional_agents=["B"])` and `start_session(name="B")` succeed
- [ ] When `A` emits `【TO:B】hello【/TO:B】`, the message text is delivered into `B`'s tmux session as a stdin write
- [ ] `state.chat` gains a `ChatMessage { kind="peer", from_agent="A", to_agent="B", text="hello", ts }`
- [ ] When `A` emits `【TO:C】...` and `C` does not exist (no AgentRecord), the parser hard-fails and `A` receives `<moderator-info kind="parse-warning" detail="unknown peer C">`; no chat entry is written
- [ ] When `A` emits `【TO:B】...` but `B.additional_agents` does NOT contain `A`, the message is rejected the same way (one-way allowlist)
- [ ] When `A` emits `【TO:moderator】...`, the parser hard-fails with `parse-warning` (moderator-as-target forbidden — they should use `【求助人类】`)
- [ ] When `A` emits `【TO:B】...` and `B.state` is `offline` / `stopped` / `error`, the message is rejected and `A` receives `<moderator-info kind="undelivered" target="B">`; the chat entry is written but marked `(undelivered)`
- [ ] When the parser sees a nested opener inside a marker body (e.g. `【TO:B】【进度汇报】nested【/进度汇报】 text【/TO:B】`), the inner one is ignored with a parse-warning; the outer marker survives
- [ ] `start_session(name="A")` a second time (same name, any host) returns `AgentAlreadyExists(name="A", host=<existing>, state=<existing>)`
- [ ] User stories covered: A.3, A.5, B.33, B.42, B.45, B.46, B.49
- [ ] ADRs respected: ADR-0005 (one-way allowlist + 同名拒绝), ADR-0010 (routing / nesting / `TO:moderator` ban / undelivered)