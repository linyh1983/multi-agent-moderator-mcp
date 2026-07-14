# 07 — Audit export + marker rate + dedup backpressure

**What to build:** moderators can dump state to JSONL for postmortem; one Agent cannot starve the others with high marker volume; duplicate markers are silently dropped; oversized markers are truncated with feedback.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] `moderator state-inspect --format jsonl` emits newline-delimited JSON covering all 5 top-level collections (one JSON object per line)
- [ ] `--agent=<name>` filters to entries related to that Agent
- [ ] `--since=<ISO-8601>` and `--until=<ISO-8601>` filter by timestamp
- [ ] Output pipes cleanly through `jq` / `grep` / `awk`
- [ ] A worker processing markers from the same Agent enforces ≤ 10 markers/sec (implementation detail: e.g. `await asyncio.sleep(0.1)` between parse+dispatch). Over-limit markers stay in `partial_buffer` and are processed next cycle — never dropped
- [ ] There is **no** global rate limit (one chatty Agent must not slow others)
- [ ] A marker whose body sha256 (`opener+content+closer`) matches a hash in `seen_marker_hashes` is silently dropped (no state mutation, no `check` entry)
- [ ] `seen_marker_hashes` is a global FIFO of 5000; oldest hash is evicted when full
- [ ] When a marker's content exceeds 64 KiB (UTF-8), it is truncated to 64 KiB and the Agent receives `<moderator-info kind="truncated" marker="…">`
- [ ] When a marker's `partial_buffer` grows past 64 KiB without a matching closer, the buffer is dropped and the Agent receives `<moderator-info kind="parse-warning">`
- [ ] User stories covered: A.14, B.43, B.44, B.48, D.69, D.70, D.71
- [ ] ADRs respected: ADR-0010 (size cap / parse-warning), ADR-0013 (dedup / per-agent rate / `state-inspect --format jsonl`)