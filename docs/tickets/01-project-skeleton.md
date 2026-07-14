# 01 — Project skeleton + state + 6-tool stubs

**What to build:** the minimum runnable skeleton — `python -m moderator serve` boots a stdio MCP server, all 6 tools are registered (stubs OK), state JSON is created with cross-process file lock, `state-inspect` CLI works.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `pip install -e ".[dev]"` succeeds and registers `moderator` + `moderator-mcp` console scripts
- [ ] `python -m moderator serve` spawns the stdio MCP server without crashing
- [ ] When registered in a Claude Code session, the 6 tools (`start_session`, `check`, `approve`, `reject`, `cue`, `stop_session`) are visible in the tools list
- [ ] `check()` against an empty state returns a one-line response referencing empty state (e.g. `(no agents; use start_session to begin)`)
- [ ] `start_session(...)` against a fake host returns a clean error string (no stacktrace); an error-state record is on disk afterwards
- [ ] `python -m moderator state-inspect` prints the JSON file at the default path; `--format jsonl` emits newline-delimited JSON
- [ ] State file write/read survives concurrent processes (file lock test)
- [ ] State file contains top-level `schema_version: 1` and 5 empty collections: `agents`, `actions`, `chat`, `progress`, `help_requests`
- [ ] User stories covered: A.1 (skeleton), A.7, A.8, A.14 (CLI), C.50, C.51, C.52, C.54
- [ ] ADRs respected: ADR-0002 (stdio), ADR-0004 (JSON + file lock), ADR-0014 (schema_v1)