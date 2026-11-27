"""MCP server smoke: initialize, tools/list, and one rocky_exec call.

No live model. The exec path is mocked. A subprocess check proves `rockycode mcp`
speaks MCP on stdout (no banner, no API key) and lists rocky_exec with the
read / deepseek-flash defaults. An in-process session then calls the tool.
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_HOME = tempfile.mkdtemp(prefix="rockymcp-home-")
os.environ["ROCKYCODE_HOME"] = _HOME
# Distinctive, and shaped like a real key so both scrub passes must catch it.
SECRET = "sk-test-fake-DO-NOT-LEAK"
os.environ["ROCKYCODE_API_KEY"] = SECRET

REPO = Path(__file__).resolve().parent.parent

import anyio  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.shared.message import SessionMessage  # noqa: E402

from rockycode.headless import EXIT_DONE  # noqa: E402
from rockycode.mcp_server import (  # noqa: E402
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    INSTRUCTIONS,
    run_delegated,
    mcp,
)
from rockycode.pricing import UsageLedger  # noqa: E402


USAGE = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 0}


def _readline(proc: subprocess.Popen, timeout: float = 15) -> str:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if proc.poll() is not None and not ready:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"mcp server exited {proc.returncode}: {err}")
        if not ready:
            continue
        line = proc.stdout.readline()
        if line:
            return line
    err = ""
    if proc.stderr:
        # non-blocking peek is enough to show why we stalled
        ready, _, _ = select.select([proc.stderr], [], [], 0)
        if ready:
            err = proc.stderr.read()
    raise AssertionError(f"timed out waiting for mcp stdout. stderr={err!r}")


def _assert_clean(blob: str) -> None:
    """The protocol stream and tool payload must not carry the key or a banner."""
    assert SECRET not in blob, "API key leaked into MCP output"
    assert "sk-" not in blob
    assert "i learn traditional physics" not in blob


def test_stdio_initialize_and_tools_list() -> None:
    """The CLI entry point: stdout is MCP JSON only, defaults stay read / flash."""
    env = os.environ.copy()
    env["ROCKYCODE_HOME"] = _HOME
    env["ROCKYCODE_API_KEY"] = SECRET
    proc = subprocess.Popen(
        [sys.executable, "-m", "rockycode.cli", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=REPO, env=env,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        }) + "\n")
        proc.stdin.flush()
        raw = _readline(proc)
        _assert_clean(raw)
        init = json.loads(raw)
        assert init["jsonrpc"] == "2.0" and init["id"] == 1, init
        result = init["result"]
        assert result["serverInfo"]["name"] == "rocky", result
        assert "profile is read" in result.get("instructions", ""), result.get("instructions")
        assert result["instructions"] == INSTRUCTIONS

        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }) + "\n")
        proc.stdin.flush()

        listed = None
        for _ in range(6):
            raw = _readline(proc)
            _assert_clean(raw)
            msg = json.loads(raw)
            if msg.get("id") == 2:
                listed = msg
                break
        assert listed is not None, "tools/list got no response"
        tools = listed["result"]["tools"]
        assert {"rocky_exec", "rocky_capabilities", "rocky_submit"} <= {t["name"] for t in tools}, tools
        schema = next(t for t in tools if t["name"] == "rocky_exec")["inputSchema"]
        assert schema["properties"]["profile"]["default"] == "read"
        assert schema["properties"]["model"]["default"] == "deepseek-flash"
        assert "task" in schema["required"]
        assert "profile" not in schema.get("required", [])
        assert "model" not in schema.get("required", [])
    finally:
        proc.kill()
        proc.communicate(timeout=5)


async def _fake_exec(**kwargs):
    _fake_exec.calls.append(kwargs)  # type: ignore[attr-defined]
    write = kwargs["write"]
    write({
        "type": "meta",
        "schema": "rockyexec/1",
        "session": "rk_smoke",
        "profile": {"name": kwargs["profile"]},
    })
    write({
        "type": "result",
        "status": "done",
        "session": "rk_smoke",
        "summary": f"translated. key was {SECRET}",
        "blocked_on": None,
        "evidence": {
            "files_changed": [],
            "commands": [{"command": f"echo {SECRET}", "ok": True}],
            "refused": [],
        },
        "steps": 2,
        "usage": dict(USAGE),
    })
    return EXIT_DONE


_fake_exec.calls = []  # type: ignore[attr-defined]


async def _with_client(callback):
    """Drive the real FastMCP server over in-memory streams. No subprocess, no model."""
    c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage](64)
    s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](64)
    server = mcp._mcp_server
    init_opts = server.create_initialization_options()

    async with anyio.create_task_group() as tg:
        tg.start_soon(server.run, c2s_recv, s2c_send, init_opts, False)
        try:
            async with ClientSession(s2c_recv, c2s_send) as session:
                await callback(session)
        finally:
            tg.cancel_scope.cancel()


def test_protocol_rocky_exec_is_mocked() -> None:
    """initialize + tools/list + rocky_exec, with run_exec replaced."""

    async def drive(session: ClientSession) -> None:
        init = await session.initialize()
        assert init.serverInfo.name == "rocky"
        assert init.instructions == INSTRUCTIONS

        listed = await session.list_tools()
        assert {"rocky_exec", "rocky_capabilities", "rocky_submit"} <= {t.name for t in listed.tools}

        _fake_exec.calls.clear()  # type: ignore[attr-defined]
        result = await session.call_tool("rocky_exec", {"task": "translate the document"})
        assert result.isError is False, result
        text = result.content[0].text
        _assert_clean(text)
        body = result.structuredContent
        assert isinstance(body, dict), result
        blob = json.dumps(body)
        _assert_clean(blob)
        assert body["status"] == "done"
        assert body["exit_code"] == 0
        assert body["profile"] == "read"  # omitted argument must not escalate
        assert body["model"] == DEFAULT_MODEL
        assert body["session"] == "rk_smoke"
        assert "translated" in body["summary"]
        assert body["evidence"]["commands"][0]["command"] == "echo [redacted]"
        assert body["steps"] == 2
        path = body["output_path"]
        assert path and os.path.isfile(path)
        try:
            disk = Path(path).read_text(encoding="utf-8")
            _assert_clean(disk)
            assert "translated" in disk
        finally:
            os.unlink(path)
        ledger = UsageLedger()
        ledger.add(DEFAULT_MODEL, USAGE)
        assert body["cost_usd"] == ledger.cost("usd")
        assert isinstance(body["peak"], bool)

        calls = _fake_exec.calls  # type: ignore[attr-defined]
        assert len(calls) == 1
        assert calls[0]["profile"] == "read"
        assert calls[0]["model"] == DEFAULT_MODEL
        assert calls[0]["max_steps"] == DEFAULT_MAX_STEPS
        assert calls[0]["sandbox"] is True
        assert calls[0]["network"] is False
        assert calls[0]["events"] is False

        # Explicit profiles are forwarded. They are not rewritten back to read,
        # and they are not the default.
        for profile in ("write", "full"):
            _fake_exec.calls.clear()  # type: ignore[attr-defined]
            again = await session.call_tool("rocky_exec", {
                "task": "apply the mechanical rename",
                "profile": profile,
            })
            assert again.isError is False, again
            assert again.structuredContent["profile"] == profile
            assert _fake_exec.calls[-1]["profile"] == profile  # type: ignore[attr-defined]
            out = again.structuredContent.get("output_path")
            if out and os.path.isfile(out):
                os.unlink(out)

    with patch("rockycode.headless.run_exec", _fake_exec):
        asyncio.run(_with_client(drive))


def test_profile_never_escalates_without_an_argument() -> None:
    async def fake(**kwargs):
        fake.calls.append(kwargs["profile"])  # type: ignore[attr-defined]
        kwargs["write"]({"type": "result", "status": "done", "summary": "ok", "usage": {}})
        return EXIT_DONE

    fake.calls = []  # type: ignore[attr-defined]

    async def go():
        with patch("rockycode.headless.run_exec", fake):
            missing = await run_delegated("  look around  ")
            assert missing.profile == DEFAULT_PROFILE == "read"
            assert missing.exit_code == 0
            blank = await run_delegated("look", profile="")
            assert blank.profile == "read"
            explicit = await run_delegated("look", profile=" WRITE ")
            assert explicit.profile == "write"
            refused = await run_delegated("look", profile="yolo")
            empty = await run_delegated("   ")
            steps = await run_delegated("look", max_steps=0)
        assert fake.calls == ["read", "read", "write"]  # type: ignore[attr-defined]
        assert refused.exit_code == 1 and "profile" in (refused.error or "")
        assert empty.exit_code == 1 and steps.exit_code == 1
        assert SECRET not in (missing.summary + (missing.error or ""))
        for result in (missing, blank, explicit):
            if result.output_path and os.path.isfile(result.output_path):
                os.unlink(result.output_path)

    asyncio.run(go())


def test_plugin_skeleton() -> None:
    plugin = json.loads((REPO / "integrations/codex-plugin/plugin.json").read_text())
    mcp_json = json.loads((REPO / "integrations/codex-plugin/mcp.json").read_text())
    market = json.loads((REPO / ".agents/plugins/marketplace.json").read_text())
    skill = REPO / "integrations/codex-plugin/skills/rocky-delegate/SKILL.md"
    assert plugin["$schema"].endswith("/plugin.schema.json")
    assert plugin["name"] == "rocky"
    assert mcp_json["$schema"].endswith("/mcp.schema.json")
    server = mcp_json["mcpServers"]["rocky"]
    assert server["type"] == "stdio"
    assert server["command"] == "rockycode"
    assert server["args"] == ["mcp"]
    assert "env" not in server  # a plugin must not ship a key
    assert market["name"] == "rockycode"
    entry = market["plugins"][0]
    assert entry["name"] == "rocky"
    assert entry["source"]["path"] == "./integrations/codex-plugin"
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_USE"
    text = skill.read_text(encoding="utf-8")
    assert "rocky_exec" in text and "profile" in text and "read" in text
    assert "gpt-6-luna" in text and "DeepSeek" in text


def test_no_key_is_an_envelope_not_a_traceback() -> None:
    saved = os.environ.pop("ROCKYCODE_API_KEY", None)
    try:
        async def go():
            # A second import of onboarding sees the env at call time.
            return await run_delegated("summarize")

        result = asyncio.run(go())
    finally:
        if saved is not None:
            os.environ["ROCKYCODE_API_KEY"] = saved
    assert result.exit_code == 1
    assert result.status == "error"
    assert "ROCKYCODE_API_KEY" in (result.error or "")
    assert SECRET not in (result.error or "")
    assert result.output_path is None


if __name__ == "__main__":
    test_stdio_initialize_and_tools_list()
    print("stdio handshake ok")
    test_protocol_rocky_exec_is_mocked()
    print("protocol rocky_exec ok")
    test_profile_never_escalates_without_an_argument()
    print("profile guard ok")
    test_plugin_skeleton()
    print("plugin skeleton ok")
    test_no_key_is_an_envelope_not_a_traceback()
    print("MCP SERVER SMOKE OK — amaze!")
