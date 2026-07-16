# 12 — SSH connect + ~/.ssh/config resolution (ticket 09 follow-up)

**What to build:** two contract gaps surfaced by manual integration
testing against `django-app-openeuler-service-10` on 2026-07-16.

The full bug report lives in `tmp_manual/TEST_PLAN.md` (Bug B1 + B2).
The `ParamikoSshDriver` lands in ticket 09 (commit `49fac0a`), but
two contracts that the runtime relies on were never implemented:

1. **B1 — `ParamikoSshDriver.connect(host)` is never invoked.**
   `runtime.py` builds the driver in lazy mode (no host) and the
   docstring promises "The runtime calls `connect(host)` at
   `start_session` time". But a `grep` over `src/` for
   `\.connect(host|ssh\.connect|_ssh\.connect` returns **zero
   matches**. `_start_one` hands `ssh` straight to `put_file`,
   which `_require_client()` rejects because `_client is None`.
   Symptom: `ParamikoSshDriver(None): not connected (called after close)`.

2. **B2 — `ParamikoSshDriver._connect()` does not resolve `~/.ssh/config` aliases.**
   `paramiko.SSHClient.connect()` does NOT auto-consult
   `~/.ssh/config`. The alias string
   (`django-app-openeuler-service-10`) is treated as a literal
   hostname → DNS lookup fails with
   `gaierror: getaddrinfo failed`. Probes confirm:
   `paramiko.config.SSHConfig.from_path(...).lookup(alias)`
   resolves to `hostname=192.168.1.52, user=django-app,
   identityfile=.../django_id_rsa`, but the driver never reads it.

**Why this matters:** ticket 09's commit message claims
"ParamikoSshDriver + RemoteTmuxDriver + ssh wiring", but the
wiring is incomplete — `start_session` against a real remote host
crashes before any tmux activity. The `MODERATOR_DRIVER=ssh`
production path is unusable until both fixes land. B1 and B2
share the same root cause (lazy SSH driver wiring not finished);
fixing only one still fails end-to-end.

**Blocked by:** 09 (driver class exists; needs runtime hookup)

**References:** issues/9 (original ticket 09), tmp_manual/TEST_PLAN.md
(B1 + B2 sections).

**Status:** ready-for-agent

## Scope

### B1 fix — call `connect(host)` at start_session time

In `src/moderator/tools/start_session.py:_start_one`, before the
first `put_file` / `run` call, invoke `ssh.connect(host)` with
the per-agent host. The runtime guarantees `ParamikoSshDriver()`
was constructed in lazy mode (no host at construction). The
`connect()` method is idempotent (`ssh.py:101-102`), so placing
it at the top of `_start_one` is safe even if a future change
constructs the driver eagerly.

`RemoteTmuxDriver` does not need a separate hookup — it
delegates to the SSH driver via `exec_via`, so once `ssh` is
connected the tmux operations work.

### B2 fix — resolve `~/.ssh/config` in `_connect()`

In `src/moderator/drivers/ssh.py:_connect()`, before
`client.connect(self._host)`:

1. Read `~/.ssh/config` via `paramiko.config.SSHConfig.from_path(...)`.
2. `lookup(self._host)` to get the merged config block.
3. Pass `hostname`, `username`, `port`, `key_filename` (first
   element of `identityfile` list) to `client.connect()`.
4. If the lookup returns nothing (no entry in `~/.ssh/config`),
   fall back to the current behaviour: treat `_host` as a literal
   hostname and let paramiko default auth + agent handle it.
5. `IdentityFile` paths in `~/.ssh/config` use `~` and Windows
   paths (`C:/...`); expand `~` via `pathlib.Path.expanduser()`
   and pass the absolute path.

Out of scope for v1: ProxyCommand, Match blocks, Include directives.
The user's verified dev host (`192.168.1.52`) uses none of these.

### Regression tests (must fail before fix, pass after)

Two tests that would have caught this:

1. **`tests/tools/test_start_session.py:test_start_session_via_ssh_invokes_connect`**
   — drive `call_tool("start_session", ...)` against a
   `RecordingSshDriver` that captures every method call. Assert
   `connect(host=...)` is called before `put_file` and `run`,
   and that the host argument matches the per-agent host.

2. **`tests/drivers/test_ssh.py:test_paramiko_driver_resolves_ssh_config_alias`**
   — write a fake `~/.ssh/config` to a tmp dir, point
   `SSH_CONFIG_PATH` at it (or monkey-patch), construct
   `ParamikoSshDriver()` with the alias, call `connect(alias)`,
   and assert the underlying `paramiko.SSHClient.connect` was
   called with the resolved `hostname=192.168.1.52`,
   `username=django-app`, `port=22`, `key_filename=<absolute path>`.
   Use the standard `mock.patch("paramiko.SSHClient.connect")`
   pattern.

The integration assertion that proves both fixes work is the
manual `R2-S1` step in `tmp_manual/TEST_PLAN.md`: a real
`mcp__moderator__start_session` call against
`django-app-openeuler-service-10` returns `isError=false`.

## Acceptance

- `tests/tools/test_start_session.py::test_start_session_via_ssh_invokes_connect` passes
- `tests/drivers/test_ssh.py::test_paramiko_driver_resolves_ssh_config_alias` passes
- All existing 281+ tests still green
- `mypy` clean on the touched modules
- Manual `R2-S1` through `R2-S8` from `tmp_manual/TEST_PLAN.md` complete against `django-app-openeuler-service-10`