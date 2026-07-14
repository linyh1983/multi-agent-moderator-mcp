# 02 — Hello Agent minimal (start_session + progress marker)

**What to build:** an end-to-end path from `start_session` to a `【进度汇报】` arriving in `state.progress`. The remote Agent is a real Claude Code session in a tmux session on the test host; its first action is to emit one progress marker.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] `start_session(name, host, project_dir, role_prompt, additional_agents?)` opens an SSH connection to `host`, creates a tmux session, SFTPs `role-<name>.txt` to `/tmp/moderator/`, and launches Claude Code inside the session with the role_prompt piped in
- [ ] On success: `state.agents[name].state` transitions `starting → running` within 1–5 seconds
- [ ] `state.agents[name]` records the canonical fields: `name`, `host`, `project_dir`, `role_prompt_path`, `tmux_session`, `state`, `started_at`, `last_output_at`, `log_offset`
- [ ] On SSH / SFTP / tmux failure: an error-state record is written with `state="error"` and a descriptive `error` string; the MCP caller sees the error message (no stacktrace)
- [ ] The worker poll loop reads new stdout bytes from the tmux session via `capture-pane`, appends to per-agent buffer, and parses `【进度汇报】…【/进度汇报】` markers
- [ ] A successful marker match writes a `ProgressEntry { ts, text }` into `state.progress[name]` (no global cap yet — that's #07)
- [ ] role_prompt text is **never** persisted into state JSON (only `role_prompt_path` is)
- [ ] User stories covered: A.1, A.2, A.4, A.6, A.7, A.8, B.32, B.47
- [ ] ADRs respected: ADR-0003 (SSH + tmux), ADR-0006 (marker protocol), ADR-0008 (lifecycle), ADR-0014 (state schema)