# 12 — SSH connect + ~/.ssh/config resolution (ticket 09 follow-up)

**What to build:** contract gaps surfaced by manual integration
testing against `django-app-openeuler-service-10` on 2026-07-16.

The full bug report lives in `tmp_manual/TEST_PLAN.md`
(Bug B1 + B2 + B3). The `ParamikoSshDriver` lands in ticket 09
(commit `49fac0a`), but three contracts that the runtime relies
on were never implemented:

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

3. **B3 — `client.connect(self._host, **connect_args)` collides on `hostname`.**
   Discovered during retest of B2's fix on 2026-07-16:
   `_resolve_ssh_config()` ALWAYS sets `hostname` (falling back to
   the alias when the config omits `HostName`). The pre-fix code
   passed `self._host` as a positional arg AND `connect_args`
   spread the resolved hostname as a kwarg. paramiko's
   `SSHClient.connect` first parameter is named `hostname`, so the
   call bound both to the same parameter and raised
   `TypeError: multiple values for argument 'hostname'`. The fix
   is to drop the positional and rely on `connect_args` alone.

**Why this matters:** ticket 09's commit message claims
"ParamikoSshDriver + RemoteTmuxDriver + ssh wiring", but the
wiring is incomplete — `start_session` against a real remote host
crashes before any tmux activity. The `MODERATOR_DRIVER=ssh`
production path is unusable until all three fixes land. B1, B2,
B3 form a strict sequence — fixing only the first leaves
end-to-end broken; fixing B1+B2 but not B3 still crashes at
the SSH handshake.

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

### B3 fix — pass host only via kwargs (no positional collision)

In `src/moderator/drivers/ssh.py:_connect()`, change:

```python
# Before (BUG):
client.connect(self._host, **connect_args)
```

to:

```python
# After (FIX):
client.connect(**connect_args)
```

`connect_args` always carries `hostname` (resolver falls back to
the alias when the config has no `HostName` line). paramiko's
`SSHClient.connect` first parameter IS named `hostname` — passing
both the positional alias AND a kwarg would bind both to the
same parameter → `TypeError: multiple values for argument 'hostname'`.

The fallback path (no `~/.ssh/config` entry → empty dict) still
works because `connect_args["hostname"]` is set from the alias,
which is identical to what the positional arg would have carried.

### Regression tests (must fail before fix, pass after)

Three tests that would have caught this:

1. **`tests/tools/test_start_session_ssh_wiring.py:test_start_session_via_ssh_invokes_connect`** (B1)
   — drive `call_tool("start_session", ...)` against a
   `RecordingSshDriver` that captures every method call. Assert
   `connect(host=...)` is called before `put_file` and `run`,
   and that the host argument matches the per-agent host.

2. **`tests/drivers/test_ssh_config_resolution.py:test_paramiko_driver_resolves_ssh_config_alias`** (B2)
   — write a fake `~/.ssh/config` to a tmp dir, monkey-patch
   `SSHConfig.from_path`, construct `ParamikoSshDriver()` with
   the alias, call `connect(alias)`, and assert the underlying
   `paramiko.SSHClient.connect` was called with the resolved
   `hostname=192.168.1.52`, `username=django-app`,
   `port=22`, `key_filename=<absolute path>`.
   Plus two companion tests covering the no-config fallback and
   `IdentityFile` tilde expansion.

3. **`tests/drivers/test_ssh_config_resolution.py:test_paramiko_driver_does_not_pass_hostname_twice`** (B3)
   — same harness as #2, with a populated SSH config. Assert
   that `client.connect` was called WITHOUT positional args (the
   host appears only via `kwargs["hostname"]`). This locks down
   the call shape so any future regression that re-introduces a
   positional alias is caught immediately.

The integration assertion that proves all three fixes work is
the manual `R2-S1` step in `tmp_manual/TEST_PLAN.md`: a real
`mcp__moderator__start_session` call against
`django-app-openeuler-service-10` returns `isError=false`.

## Acceptance

- `tests/tools/test_start_session_ssh_wiring.py::test_start_session_via_ssh_invokes_connect` passes
- `tests/drivers/test_ssh_config_resolution.py::*` (4 tests) pass
- All existing 281+ tests still green (2 known failures in
  `test_local.py` are pre-existing and out of scope)
- `mypy` clean on the touched modules
- Manual `R2-S1` through `R2-S8` from `tmp_manual/TEST_PLAN.md` complete against `django-app-openeuler-service-10`