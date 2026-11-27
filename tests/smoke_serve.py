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
        [sys.executable, "-m", "rockycode.cli", "serve", "--workdir", tmpdir],
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
            assert resp.get("result", {}).get("version") == "1.0.0", \
                f"unexpected init response: {resp}"
            sid = resp.get("result", {}).get("session_id")
            assert sid

            # Session-scoped Artifact inventory exists before the first file,
            # so the VS Code tree can show the active session immediately.
            proc.stdin.write(_request("artifact/list", {"session_id": sid}, msg_id=2))
            proc.stdin.flush()
            artifacts = _read_line(proc).get("result", {})
            assert artifacts == {
                "session_id": sid,
                "server_running": False,
                "server_url": None,
                "artifacts": [],
            }, artifacts

            # Planning is a host protocol state; no model call for an empty chat.
            def plan_action(action, **extra):
                proc.stdin.write(_request(f"plan/{action}", {"session_id": sid, **extra}, msg_id=4))
                proc.stdin.flush()
                return _read_line(proc)

            state = plan_action("start")["result"]
            assert state["status"] == "planning" and not state["text"]
            plan_path = Path(tmpdir) / state["path"]
            plan_path.write_text("## Scope\nKeep changes small.\n## Verify\n- run tests")
            review = plan_action("review")["result"]
            assert review["status"] == "review" and review["revision"]
            assert "Keep changes small" in review["text"]
            assert plan_action("approve", revision="stale")["error"]["code"] == -32602
            assert plan_action("discuss")["result"]["status"] == "planning"
            assert plan_action("review")["result"]["revision"] == review["revision"]
            assert plan_action("leave")["result"]["status"] == "off"
            assert plan_action("status")["result"]["status"] == "off"
            assert plan_action("approve", revision=review["revision"])["error"]["code"] == -32602

            # Send shutdown
            proc.stdin.write(_request("shutdown", msg_id=3))
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
            assert resp.get("result", {}).get("version") == "1.0.0", \
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
    from rockycode.server import _notify, _response, _error

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
    from rockycode.server import _event_to_notification
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


def test_approval_note_notification():
    """A decision the pipeline takes without asking — a static block, or the
    verifier's auto allow/deny — reaches the client as session/approval_note,
    the serve twin of the TUI's transcript note (tui/approval.py)."""
    import asyncio
    import tempfile
    from pathlib import Path
    from rockycode import server as srv

    lines = []
    original = srv._write_line
    srv._write_line = lambda msg: lines.append(msg)
    try:
        workdir = Path(tempfile.mkdtemp())

        class ServerStub:   # the pieces _make_approver touches
            _workdir = workdir
            _perm_events: dict = {}
            _perm_results: dict = {}
            _perm_sessions: dict = {}

            def _cleanup_perm(self, eid):
                pass

        class EngineStub:
            registry: dict = {}
            allowed_roots = ()
            read_grants: set = set()

        approver = srv.SessionManager._make_approver(ServerStub(), "s1", EngineStub())
        allowed = asyncio.run(approver("bash", {"command": "sudo rm -rf /"}))
    finally:
        srv._write_line = original
    assert allowed is False, "a block-tier command is refused in every mode"
    notes = [m for m in lines if m.get("method") == "session/approval_note"]
    assert notes, lines
    params = notes[0]["params"]
    assert params["session_id"] == "s1" and params["kind"] == "blocked" and params["tool"] == "bash", params
    assert "rm -rf" in params["detail"], params
    assert not [m for m in lines if m.get("method") == "session/request_permission"], "a static block never asks"
    assert approver.refused and approver.refused[0]["reason"], approver.refused
    print("  approval_note: a static block reaches the client as a notification ✓")


def test_cli_version_flag():
    """`rockycode --version` prints the SAME version the serve handshake
    reports — both derive from package metadata, and this literal is part of
    the release bump ritual (matches the handshake asserts above)."""
    from typer.testing import CliRunner

    from rockycode.cli import app
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0 and "rockycode 1.0.0" in result.output, result.output


if __name__ == "__main__":
    test_serve_jsonrpc_handshake()
    print("PASS test_serve_jsonrpc_handshake")

    test_serve_survives_malformed_messages()
    print("PASS test_serve_survives_malformed_messages")

    test_serve_jsonrpc_protocol()
    print("PASS test_serve_jsonrpc_protocol")

    test_event_to_notification()
    print("PASS test_event_to_notification")

    test_approval_note_notification()
    test_cli_version_flag()
    print("PASS test_cli_version_flag")

    print("\nOK — all serve smoke tests passed")
