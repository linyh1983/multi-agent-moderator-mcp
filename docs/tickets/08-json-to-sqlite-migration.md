# 08 — JSON → SQLite migration + schema_version refusal

**What to build:** the persistence layer refuses to load an old-`schema_version` state, and provides a manual migration script that walks the version chain with reversible per-step functions.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] State-store loader checks `schema_version` on startup; if it is below the minimum supported version (e.g. 0 when current is 1), the server refuses to start and prints: `State schema version <N> is below minimum supported version <M>. Run: python -m state.migrate moderator_state.json`
- [ ] `state/migrate.py` exposes `migrate(state: dict, from_version: int, to_version: int) -> dict` plus a per-version step dict (e.g. `MIGRATIONS = {1: v1_to_v2, …}`)
- [ ] Each step function has a corresponding `MIGRATIONS_REVERSE[v]` for rollback and tests
- [ ] `python -m state.migrate <path>` runs all required steps in order, never skipping versions; writes a `moderator_state.json.bak.<ts>` backup before mutating
- [ ] When `state.actions | length ≥ 10_000` OR file size ≥ 50 MiB OR `state.chat | length ≥ 10_000`, `state-inspect` emits a migration suggestion with the suggested command
- [ ] Manual invocation: `python -m state.migrate moderator_state.json` migrates JSON → SQLite (`.db`), leaves the JSON as `.bak`, and a server reload picks up the new backend
- [ ] After migration, all 5 top-level collections are queryable via the same Pydantic models (the schema is the contract — backend changes silently)
- [ ] User stories covered: C.55, C.61, C.62, C.63, D.75
- [ ] ADRs respected: ADR-0004 (migration trigger conditions), ADR-0016 (manual migration + reversible steps)