# 踩过的坑 / Gotchas

A growing list of implementation traps that wasted real time, with the
fix already known. Each item is anchored to the ticket that
discovered it and (where it exists) the ADR that documents the chosen
pattern. Append new ones; never rewrite history.

> 当你即将引入一种新机制（锁、IPC、文件格式、并发原语）时，先 grep 这里。

---

## G-001: `msvcrt.locking` 不能跨进程阻塞 — 用 atomic-take-lockfile

**Symptom**: cross-process lock test returns in ~3 ms even though
the other process is "supposed to be" holding the lock; the second
writer doesn't actually wait.

**Root cause**: `msvcrt.locking(fd, LK_NBLCK, ...)` is **best-effort**
on Windows. Per CPython docs, it may or may not block cross-process,
depending on the file handle inheritance and Windows version. It is
documented as advisory.

**Fix**: atomic-take-lockfile algorithm. The lock IS the existence of
a sentinel file, taken via an atomic primitive:

- POSIX:  `os.link(handle, lock_path)` — fails with EEXIST if held
- Windows: `os.rename(handle, lock_path)` — raises FileExistsError
  if held

Implementation: `src/moderator/state/store.py::StateLock`.
ADR: **ADR-0004**.

**Discovered in**: ticket 01.

---

## G-002: 同进程内 `with state_lock(): write_state()` 会自死锁

**Symptom**: the same process tries to take the state lock twice
nested; the inner call sits forever (or until backoff gives up).

**Root cause**: `write_state` (and `read_state`) acquire
`StateLock` internally. If the caller also wraps the call in
`with state_lock(...):`, the second `__enter__` waits on the
first's `__exit__` — which is queued behind the inner call's
release, which is queued behind the second's acquire. Classic
reentrant-lock bug.

**Fix**: never call `read_state` / `write_state` from inside an
existing `with state_lock:` block. If you need to compose, use the
unlocked helpers (`_read_unlocked` / `_write_unlocked`) and document
the invariant.

**Discovered in**: ticket 01.

---

## G-003: Windows `multiprocessing.spawn` 启动慢，别用 fixed sleep 同步

**Symptom**: cross-process test sees the writer return in 5 ms,
suggesting the lock wasn't held. Elapsed-time assertion (`>= 0.4s`)
fails.

**Root cause**: on Windows, `spawn` re-imports the target module
fresh in the child process; cold-start overhead is hundreds of
milliseconds, not the tens you'd see on Linux with `fork`. A
`time.sleep(0.15)` after `Process.start()` is not enough.

**Fix**: use a **ready-file** synchronization pattern. The holder
process touches a file once it has acquired the lock; the parent
polls for that file (with a generous timeout) before measuring
elapsed time. The first 5ms then reliably overlaps the actual
hold window.

```python
# holder child
with state_lock(path):
    Path(ready_str).write_text("ready")
    time.sleep(hold_seconds)

# parent
a.start()
assert _wait_for(ready, timeout=5.0)
t0 = time.monotonic()
_writer_at(path, 222)  # blocks until holder releases
elapsed = time.monotonic() - t0
```

**Discovered in**: ticket 01.

---

## G-004: MCP Python SDK stdio 走 NDJSON，不是 LSP 的 Content-Length 帧

**Symptom**: server logs `Invalid JSON: expected value at line 1
column 1 [input_value='Content-Length: 161\n']` and falls into the
exception handler. Every send is rejected as malformed.

**Root cause**: there are two framing conventions in MCP
implementations. The Python `mcp.server.stdio.stdio_server` reads
stdin **line-by-line** and `JSONRPCMessage.model_validate_json` on
each line (see `Lib/site-packages/mcp/server/stdio.py::stdin_reader`).
The TypeScript / Rust SDKs use LSP-style `Content-Length:` headers.

**Fix**: when driving the Python SDK over a real pipe, send one
JSON object per line, terminated with `\n`. No header. Tests that
need to talk to the server (e.g. `tests/test_serve.py`) must use
this framing, not LSP framing.

**Discovered in**: ticket 01.

---

## G-005: Pydantic `extra="forbid"` 意味着字段名必须严格匹配 schema

**Symptom**: `ValidationError: 3 validation errors for AgentRecord
... last_seen / role_prompt / metadata — Extra inputs are not
permitted`. The code is "obviously" setting valid fields.

**Root cause**: the schema in `core/models.py` is locked down with
`model_config = ConfigDict(extra="forbid")` and uses specific field
names:

- `started_at`, `last_output_at` (not `last_seen`)
- `error`, `last_error` (not `metadata.error`)
- `role_prompt_path` (not `role_prompt` — the prompt lives on disk)

If you add a field to a record at a call site, it has to exist on
the model. If you think the model is missing a field, that's a
schema decision, not a bug to paper over with `extra="allow"`.

**Fix**: when building state records, look up the model first.
The `core/models.py` source of truth is mirrored in
`docs/glossary.md` §4 but the source wins.

**Discovered in**: ticket 01.

---

## G-006: PowerShell `git -m @'...'@` 把字面 `@'` 塞进 commit subject

**Symptom**: `git log` shows commit subject like
`@feat(scope): ...` with a leading `@`. The closing `@` from the
heredoc delimiter also lands on its own line at the end of the body.

**Root cause**: PowerShell's here-string `@'...'@` is parsed by
PowerShell BEFORE git sees the argument. The leading `@'` is the
opening delimiter and gets passed as part of the literal string
to `-m`. Different shells treat this differently — bash and zsh
strip the delimiters correctly.

**Fix**: pipe the commit message via stdin instead of `-m`:

```bash
git commit -F - <<'EOF'
feat(scope): subject

body...

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

`<<'EOF'` (single-quoted) prevents any expansion inside the body.
`git commit -F -` reads the message from stdin.

**Discovered in**: ticket 01 (commit ae52cd5 → amended to b3909e9).

---

## Add a new gotcha

1. Write a Symptom — paste the actual error / failed assertion.
2. Write a Root cause — explain WHY the symptom happens, not just
   that it does.
3. Write a Fix — link the code (file:line or symbol).
4. Set Discovered in — the ticket where it cost real time.
5. If it generalizes beyond one ticket, link the ADR that captured
   the chosen pattern.

A gotcha that doesn't generalize into a pattern is just a bug; close
it instead. A gotcha that doesn't have a fix yet is a follow-up —
file a ticket, don't write it here.
