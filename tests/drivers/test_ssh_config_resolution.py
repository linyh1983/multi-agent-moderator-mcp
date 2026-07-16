"""Regression test for ticket 12 / bug B2.

``ParamikoSshDriver._connect()`` previously called
``client.connect(self._host)`` with the alias string verbatim.
paramiko's SSHClient does NOT consult ``~/.ssh/config``, so the
alias (``django-app-openeuler-service-10``) was treated as a
literal hostname and DNS lookup failed with
``gaierror: getaddrinfo failed``.

This test pins the contract: when ``_connect`` runs with a host
that has an entry in ``~/.ssh/config``, the underlying
``paramiko.SSHClient.connect`` must receive the resolved
``hostname``, ``username``, ``port``, and ``key_filename`` — NOT
the raw alias string.

Implementation note: ``paramiko.config.SSHConfig.from_path``
takes a string path. The driver reads from the user's
``~/.ssh/config`` directly. To make this test deterministic
without touching the real ``~/.ssh/config``, we monkeypatch the
driver's config-reader to return our own ``SSHConfig`` instance
loaded from a tmp file (mimicking the paramiko API).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko

from moderator.drivers.ssh import ParamikoSshDriver


_FAKE_CONFIG = """\
Host django-app-openeuler-service-10
    HostName 192.168.1.52
    User django-app
    Port 22
    IdentityFile C:/Users/clark/Keys/OpenEuler/django_id_rsa

Host other-host
    HostName 10.0.0.1
    User otheruser
"""


def _build_ssh_config_from_text(text: str) -> paramiko.config.SSHConfig:
    """Helper: parse SSH-config text via the same machinery paramiko uses."""
    import io

    cfg = paramiko.config.SSHConfig()
    cfg.parse(io.StringIO(text))
    return cfg


def test_paramiko_driver_resolves_ssh_config_alias(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """When the alias resolves to an entry in ``~/.ssh/config``, the
    driver must call ``paramiko.SSHClient.connect`` with the
    RESOLVED hostname / username / port / key_filename, not the raw
    alias string."""
    fake_cfg_path = tmp_path / "ssh_config"
    fake_cfg_path.write_text(_FAKE_CONFIG, encoding="utf-8")
    fake_ssh_config = _build_ssh_config_from_text(_FAKE_CONFIG)

    # Force the driver to read our tmp config, not the user's real one.
    monkeypatch.setattr(
        "moderator.drivers.ssh.paramiko.config.SSHConfig.from_path",
        lambda _p: fake_ssh_config,
    )

    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        ParamikoSshDriver(host="django-app-openeuler-service-10")

        # The driver must have called connect with the RESOLVED args.
        assert client.connect.called, "SSHClient.connect was never called"
        kwargs = client.connect.call_args.kwargs
        # hostname must be the resolved IP, not the alias.
        assert kwargs.get("hostname") == "192.168.1.52", (
            f"expected resolved hostname=192.168.1.52, got {kwargs.get('hostname')!r}"
        )
        assert kwargs.get("username") == "django-app"
        assert kwargs.get("port") == 22
        # IdentityFile path with ~ or Windows form must be absolute.
        key = kwargs.get("key_filename")
        assert key is not None, "key_filename was not passed"
        assert Path(key).is_absolute(), (
            f"key_filename must be absolute, got {key!r}"
        )


def test_paramiko_driver_falls_back_when_alias_not_in_config(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """If the host is NOT in ``~/.ssh/config`` (e.g. raw IP), the driver
    must fall back to passing the host string verbatim — paramiko's
    defaults (agent, common keys) handle auth in that case."""
    # Empty config: nothing resolves.
    empty_cfg = _build_ssh_config_from_text("# no hosts\n")
    monkeypatch.setattr(
        "moderator.drivers.ssh.paramiko.config.SSHConfig.from_path",
        lambda _p: empty_cfg,
    )

    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        ParamikoSshDriver(host="10.0.0.99")

        # Falls back: connect called with the raw host.
        assert client.connect.called
        # Either positional or hostname kwarg carries the raw IP.
        if client.connect.call_args.kwargs:
            assert client.connect.call_args.kwargs.get("hostname") == "10.0.0.99"
        else:
            assert client.connect.call_args.args[0] == "10.0.0.99"


def test_paramiko_driver_expands_tilde_in_identityfile(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """``IdentityFile`` paths in ``~/.ssh/config`` may use ``~``. The
    driver must expand ``~`` to the user's home so paramiko does
    not pass a literal ``~`` to the OS."""
    cfg_text = """\
Host alias-with-tilde
    HostName 10.0.0.5
    User someuser
    IdentityFile ~/.ssh/test_id_rsa
"""
    fake_ssh_config = _build_ssh_config_from_text(cfg_text)
    monkeypatch.setattr(
        "moderator.drivers.ssh.paramiko.config.SSHConfig.from_path",
        lambda _p: fake_ssh_config,
    )

    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        ParamikoSshDriver(host="alias-with-tilde")

        key = client.connect.call_args.kwargs.get("key_filename")
        assert key is not None
        assert "~" not in key, f"~ must be expanded, got {key!r}"
        assert Path(key).is_absolute()


def test_paramiko_driver_does_not_pass_hostname_twice(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """Regression for ticket 12 / bug B3.

    ``_connect()`` must NOT pass ``hostname`` to
    :meth:`paramiko.SSHClient.connect` twice — once as a positional
    arg, once via the resolved config kwargs. The pre-fix code did
    ``client.connect(self._host, **connect_args)``, where
    ``connect_args`` always carried a ``hostname`` key (falling
    back to the alias when the config lacked a ``HostName`` line).
    That triggered ``TypeError: multiple values for argument
    'hostname'`` at runtime against any host with a matching
    ``Host`` block in ``~/.ssh/config`` — i.e. exactly the
    scenario B2 was supposed to enable.

    paramiko's :meth:`SSHClient.connect` signature has ``hostname``
    as the first positional parameter. So ``connect(alias, **cfg)``
    where ``cfg`` already contains ``hostname=<resolved>`` binds
    BOTH to paramiko's ``hostname`` parameter — a Python-level
    TypeError before the call body even runs. The fix is to drop
    the positional and rely on ``connect_args`` alone; this test
    asserts the alias does NOT leak through as a positional arg
    when a config entry exists."""
    cfg_text = """\
Host django-app-openeuler-service-10
    HostName 192.168.1.52
    User django-app
    Port 22
    IdentityFile /tmp/fake_id_rsa
"""
    fake_ssh_config = _build_ssh_config_from_text(cfg_text)
    monkeypatch.setattr(
        "moderator.drivers.ssh.paramiko.config.SSHConfig.from_path",
        lambda _p: fake_ssh_config,
    )

    with patch("moderator.drivers.ssh.paramiko.SSHClient") as ClientCls:
        client = ClientCls.return_value
        ParamikoSshDriver(host="django-app-openeuler-service-10")

        assert client.connect.called, "SSHClient.connect was never called"
        positional = client.connect.call_args.args
        kwargs = client.connect.call_args.kwargs

        # The resolved hostname MUST appear via kwargs (paramiko's
        # first parameter is named ``hostname``).
        assert kwargs.get("hostname") == "192.168.1.52", (
            f"resolved hostname missing or wrong; "
            f"call_args={client.connect.call_args!r}"
        )
        # The raw alias MUST NOT be passed positionally — that's
        # exactly what would collide with kwargs["hostname"] inside
        # paramiko and raise ``multiple values for argument
        # 'hostname'``.
        assert not positional, (
            f"connect() must not be called with positional args "
            f"when the SSH config provides hostname; got "
            f"positional={positional!r}, kwargs={kwargs!r}"
        )