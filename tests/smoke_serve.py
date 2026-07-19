"""Smoke test for rockycode serve — JSON-RPC 2.0 over stdio subprocess.

No real API key needed; we intercept with a fake engine by checking that
the server starts and responds to initialize / cancel / shutdown correctly.

For a proper integration test, set ROCKYCODE_MODEL and run with a real key.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _rockycode(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "rockycode.cli"] + args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=REPO_ROOT,
    )


def _request(method: str, params: dict = None, msg_id: int = 1) -> str:
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    return json.dumps(msg) + "\n"


def _read_line(proc: subprocess.Popen) -> dict:
    line = proc.stdout.readline()
    if not line:
        raise EOFError("server closed stdout")
    return json.loads(line)


# Fake creds so the handshake is deterministic regardless of the runner's env:
# a model is required, and initialize builds an AsyncOpenAI client (needs a key
# present, never actually called — initialize makes no request).
_FAKE_ENV = {**os.environ, "ROCKYCODE_API_KEY": "sk-test-fake", "ROCKYCODE_MODEL": "fake-model"}


def _serve(tmpdir: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "rockycode/cli.py", "serve", "--workdir", tmpdir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=REPO_ROOT, env=_FAKE_ENV,
    )


def _drain(proc: subprocess.Popen) -> None:
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def test_serve_jsonrpc_handshake():
    """Send an initialize request and verify the response."""
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = _serve(tmpdir)
        try:
            # Send initialize
            proc.stdin.write(_request("initialize"))
            proc.stdin.flush()
            resp = _read_line(proc)
            assert resp.get("result", {}).get("version") == "0.1.0", \
                f"unexpected init response: {resp}"
            assert "session_id" in resp.get("result", {})

            # Send shutdown
            proc.stdin.write(_request("shutdown", msg_id=2))
            proc.stdin.flush()
            resp = _read_line(proc)
            assert resp.get("result", {}).get("ok") is True

        finally:
            _drain(proc)


def test_serve_survives_malformed_messages():
    """A hostile/buggy client sending non-JSON and valid-JSON-non-objects must
    NOT kill the server — it should keep answering. Guards the DoS where a bare
    `42\\n` used to crash the dispatch loop on msg.get(...)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = _serve(tmpdir)
        try:
            for bad in ("not json at all\n", "42\n", '"a string"\n', "null\n", "[]\n"):
                proc.stdin.write(bad)
            proc.stdin.flush()
            # Still alive? initialize must succeed after the garbage.
            proc.stdin.write(_request("initialize"))
            proc.stdin.flush()
            resp = _read_line(proc)
            assert resp.get("result", {}).get("version") == "0.1.0", \
                f"server did not survive malformed input: {resp}"
            proc.stdin.write(_request("shutdown", msg_id=2))
            proc.stdin.flush()
            _read_line(proc)
        finally:
            _drain(proc)


def test_serve_jsonrpc_protocol():
    """Test the JSON-RPC protocol handshake with a fake engine.

    Since we can't easily mock the Engine in a subprocess, we test:
    1. The module imports cleanly
    2. The server code parses JSON-RPC messages correctly
    3. The _response/_error/_notify helpers work
    """
    from rockycode.engine.server import _notify, _response, _error

    # Test notification format
    n = _notify("session/state_changed", {"session_id": "abc", "state": "thinking"})
    assert n["jsonrpc"] == "2.0"
    assert n["method"] == "session/state_changed"
    assert n["params"]["state"] == "thinking"
    assert "id" not in n  # notifications have no id

    # Test response format
    r = _response(1, {"version": "0.1.0"})
    assert r["jsonrpc"] == "2.0"
    assert r["id"] == 1
    assert r["result"]["version"] == "0.1.0"

    # Test error format
    e = _error(2, -32601, "unknown method")
    assert e["jsonrpc"] == "2.0"
    assert e["id"] == 2
    assert e["error"]["code"] == -32601
    assert "unknown method" in e["error"]["message"]


def test_event_to_notification():
    """Test that all engine event types serialize cleanly."""
    from rockycode.engine.server import _event_to_notification
    from rockycode.engine.events import (
        StateChanged, ThinkingDelta, TextDelta, ToolStarted, ToolFinished,
        TurnFinished,
    )
    from rockycode.engine import AgentState

    sid = "test-session"

    events = [
        (StateChanged(state=AgentState.THINKING), "session/state_changed"),
        (ThinkingDelta(text="hmm..."), "session/thinking_delta"),
        (TextDelta(text="hello"), "session/text_delta"),
        (ToolStarted(call_id="c1", tool="bash", args={"command": "ls"}), "session/tool_started"),
        (ToolFinished(call_id="c1", tool="bash", output="ok", ok=True, duration_s=0.5), "session/tool_finished"),
        (TurnFinished(steps=3, usage={"prompt_tokens": 100}), "session/turn_finished"),
    ]

    for ev, expected_method in events:
        n = _event_to_notification(sid, ev)
        assert n is not None, f"no notification for {type(ev).__name__}"
        assert n["method"] == expected_method, \
            f"expected {expected_method}, got {n['method']} for {type(ev).__name__}"
        assert n["params"]["session_id"] == sid


if __name__ == "__main__":
    test_serve_jsonrpc_handshake()
    print("PASS test_serve_jsonrpc_handshake")

    test_serve_survives_malformed_messages()
    print("PASS test_serve_survives_malformed_messages")

    test_serve_jsonrpc_protocol()
    print("PASS test_serve_jsonrpc_protocol")

    test_event_to_notification()
    print("PASS test_event_to_notification")

    print("\nOK — all serve smoke tests passed")
