"""LSP bridge smoke test — drives the fake LSP-over-MCP server (fake_lsp_server.py).

No real language server is involved, so this proves the bridge's own behaviour
and nothing about any wrapper. What it holds the bridge to:

- a query that worked returns its text, every block of it;
- an empty answer is `[no results]` and still a success;
- an unsupported operation, a server error, a slow server and a stopped server
  are failures, and tools.execute counts them as failures;
- nothing hangs, and nothing is ever answered with an empty string;
- the command line is parsed like a command line;
- `/lsp` tells an unconfigured user how to set it up instead of "off".

Manual check with the same fake server:
    ROCKYCODE_LSP_COMMAND="python tests/fake_lsp_server.py" rockycode chat
then ask for a symbol lookup, or type /lsp.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from rockycode.engine import tools as tools_mod
from rockycode.engine.lsp import (LSPManager, LSPUnavailable, MultiTenantLSPManager, build_lsp_tools,
                                  resolve_lsp_config)
from rockycode.i18n import tr
from rockycode.tui import commands

SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"


async def run(registry, name, **arguments):
    """Through tools.execute, the way the engine runs a tool: (text, ok)."""
    return await tools_mod.execute(registry, name, json.dumps(arguments))


async def main():
    mgr = LSPManager(sys.executable, [str(SERVER)])
    await mgr.start()
    await asyncio.wait_for(mgr.ready, timeout=30)
    registry = build_lsp_tools(mgr)

    out, ok = await run(registry, "lsp_lookup", symbol="User", action="definition")
    assert ok and out.startswith("lsp definition: User\n") and "class User" in out, out
    out, ok = await run(registry, "lsp_diagnostics", path="")
    assert ok and "models.py" in out, out  # the explicit query still works; nothing calls it for you
    out, ok = await run(registry, "lsp_lookup", symbol="User", action="implementation")
    assert ok and "class User(BaseModel)" in out and "class AdminUser(User)" in out, out
    print("lsp tools: a query that worked returns its text, every block of it  ✓")

    out, ok = await run(registry, "lsp_symbol_search", query="nothing-here")
    assert ok and out.endswith("[no results]"), out
    print("lsp tools: an empty answer is [no results] and a success  ✓")

    out, ok = await run(registry, "lsp_lookup", symbol="User", action="callers")
    assert not ok and out.startswith("[error] lsp callers: User: this lsp server has no such operation"), out
    out, ok = await run(registry, "lsp_lookup", symbol="boom", action="hover")
    assert not ok and out.startswith("[error] lsp hover: boom:") and "exploded" in out, out
    out, ok = await run(registry, "lsp_lookup", symbol="User", action="rename")
    assert not ok and out.startswith("[error] unknown action"), out
    print("lsp tools: unsupported and server errors are failures execute() can see  ✓")

    mgr.CALL_TIMEOUT_S = 0.5  # the fake server sleeps 3 s on slow.py
    out, ok = await asyncio.wait_for(run(registry, "lsp_file_symbols", path="slow.py"), timeout=10)
    assert not ok and out.startswith("[timeout] symbols in slow.py:"), out
    print("lsp tools: a slow server is a [timeout], not an empty file  ✓")

    await mgr.stop()
    for name, arguments in (("lsp_lookup", {"symbol": "User", "action": "definition"}),
                            ("lsp_symbol_search", {"query": "User"}),
                            ("lsp_file_symbols", {"path": "x.py"}), ("lsp_diagnostics", {})):
        out, ok = await asyncio.wait_for(run(registry, name, **arguments), timeout=10)
        assert not ok and out.startswith("[error]") and "not running" in out, (name, out)
    try:
        await mgr._call("definition", {"symbol": "User"})
        raise AssertionError("a stopped server must not answer with an empty string")
    except LSPUnavailable:
        pass
    print("lsp tools: after the server stops every tool says so, fast  ✓")

    # The multi-session wrapper with nothing started is "not running" too, never "no results".
    idle = MultiTenantLSPManager(sys.executable, [str(SERVER)])
    out, ok = await run(build_lsp_tools(idle), "lsp_symbol_search", query="User")
    assert not ok and "not running" in out, out

    # ROCKYCODE_LSP_COMMAND is a command line: quotes group, nothing is guessed.
    with patch.dict(os.environ, {"ROCKYCODE_LSP_COMMAND": '"/opt/my tools/lsp-mcp" --root "a b" -v'}):
        assert resolve_lsp_config() == ("/opt/my tools/lsp-mcp", ["--root", "a b", "-v"])
    with patch.dict(os.environ, {"ROCKYCODE_LSP_COMMAND": "   "}):
        assert resolve_lsp_config() is None
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ROCKYCODE_LSP_COMMAND", None)
        assert resolve_lsp_config() is None, "nothing is auto-detected"
    with patch.dict(os.environ, {"ROCKYCODE_LSP_COMMAND": 'python "unclosed'}):
        try:
            resolve_lsp_config()
            raise AssertionError("unbalanced quotes must not launch something else")
        except ValueError:
            pass
    print("lsp config: a quoted path stays one argument; bad quotes are refused  ✓")

    # /lsp: four different states, four different answers.
    said = []

    async def add(widget):
        said.append(str(widget.render()))

    def app(**engine):
        return NS(engine=NS(**engine), _t=lambda key, **params: tr(key, "en", **params), _add=add)

    await commands.cmd_lsp(app(lsp_enabled=False, lsp_manager=None, runtime=NS(lsp_error=None)), "/lsp")
    assert "--no-lsp" in said[-1], said[-1]
    await commands.cmd_lsp(app(lsp_enabled=True, lsp_manager=None, runtime=NS(lsp_error=None)), "/lsp")
    assert "ROCKYCODE_LSP_COMMAND" in said[-1] and "check_code" in said[-1] and "--no-lsp" not in said[-1], said[-1]
    await commands.cmd_lsp(app(lsp_enabled=True, lsp_manager=None,
                               runtime=NS(lsp_error="ROCKYCODE_LSP_COMMAND is not a command line (No closing quotation)")),
                           "/lsp")
    assert "not running" in said[-1] and "No closing quotation" in said[-1], said[-1]
    await commands.cmd_lsp(app(lsp_enabled=True, lsp_manager=idle, runtime=NS(lsp_error=None)), "/lsp")
    assert "not connected" in said[-1], said[-1]
    print("/lsp: off, not set up, broken and not connected each say what they are  ✓")

    print("LSP SMOKE OK — rocky no pretend. server gone, rocky say server gone. amaze!")


asyncio.run(main())
