"""End-to-end smoke test for the stdio MCP server.

Drives ``python -m moderator serve`` as a subprocess and exchanges a
handful of JSON-RPC messages with it. The Python MCP SDK's stdio
transport uses **newline-delimited JSON** (one JSON object per line),
NOT Content-Length framing — confirmed by reading
``mcp/server/stdio.py``.

Acceptance covered (ticket 01):

- "spawns the stdio MCP server without crashing" — handshake works,
  clean EOF on stdin → clean exit.
- "6 tools visible in Claude Code tools list" — tools/list returns
  exactly the 6 stub names.
- "check() against empty state returns one-line response" — drive
  tools/call over the wire and check the response content.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Framing — newline-delimited JSON (what mcp.server.stdio expects).
# ---------------------------------------------------------------------------


def _frame(message: dict) -> bytes:
    """Encode one JSON-RPC message as a single UTF-8 line + ``\\n``."""
    return (json.dumps(message) + "\n").encode("utf-8")


def _read_message(stream, timeout: float = 5.0) -> dict | None:
    """Read one newline-delimited JSON message; None on EOF."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        return json.loads(line)
    raise AssertionError("timeout waiting for message")


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _spawn_serve(
    state_path: Path, project_root: Path
) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["MODERATOR_STATE_PATH"] = str(state_path)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "moderator", "serve"],
        cwd=str(project_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


@pytest.fixture
def serve_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    project_root = Path(__file__).resolve().parent.parent
    proc = _spawn_serve(state_path, project_root)
    try:
        yield proc
    finally:
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _initialize(proc: subprocess.Popen[bytes]) -> dict:
    assert proc.stdin and proc.stdout
    proc.stdin.write(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.0.0"},
                },
            }
        )
    )
    proc.stdin.flush()
    resp = _read_message(proc.stdout)
    assert resp is not None, "no initialize response"
    assert resp.get("id") == 1, f"unexpected response: {resp}"

    # Send the initialized notification (server expects this before
    # it will accept further requests).
    proc.stdin.write(
        _frame({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    )
    proc.stdin.flush()
    return resp


# ---------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------


def test_serve_handshake_advertises_six_tools(serve_proc) -> None:
    init_resp = _initialize(serve_proc)
    assert init_resp["result"]["serverInfo"]["name"] == "moderator"

    serve_proc.stdin.write(
        _frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    )
    serve_proc.stdin.flush()

    list_resp = _read_message(serve_proc.stdout)
    assert list_resp is not None
    assert list_resp["id"] == 2
    tools = list_resp["result"]["tools"]
    names = sorted(t["name"] for t in tools)
    assert names == sorted(
        [
            "start_session",
            "check",
            "approve",
            "reject",
            "cue",
            "stop_session",
        ]
    ), f"expected 6 tools, got {names}"


def test_serve_check_returns_empty_state_one_liner(serve_proc) -> None:
    _initialize(serve_proc)

    serve_proc.stdin.write(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "check", "arguments": {}},
            }
        )
    )
    serve_proc.stdin.flush()

    resp = _read_message(serve_proc.stdout)
    assert resp is not None
    assert resp["id"] == 2
    assert "result" in resp, f"error response: {resp}"
    content = resp["result"]["content"]
    assert len(content) == 1
    assert content[0]["text"] == "(no agents; use start_session to begin)"
    assert resp["result"].get("isError") is False


def test_serve_starts_and_exits_cleanly_on_eof(tmp_path: Path, monkeypatch) -> None:
    """Acceptance: spawns the stdio MCP server without crashing."""
    state_path = tmp_path / "moderator_state.json"
    monkeypatch.setenv("MODERATOR_STATE_PATH", str(state_path))
    project_root = Path(__file__).resolve().parent.parent

    proc = _spawn_serve(state_path, project_root)
    try:
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "0"},
                    },
                }
            )
        )
        proc.stdin.flush()
        resp = _read_message(proc.stdout)
        assert resp is not None and resp.get("id") == 1
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        rc = proc.wait(timeout=3.0)
        # stderr is captured; if the server crashed it'd show a traceback.
        stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
        assert rc == 0, f"server exited rc={rc}; stderr=\n{stderr_text}"
        assert "Traceback" not in stderr_text, stderr_text