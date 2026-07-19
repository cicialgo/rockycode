"""LSP bridge smoke test — drives the fake LSP-MCP server (fake_lsp_server.py),
no real language server required.

This is also HOW TO TEST LSP MANUALLY when it's enabled: point the env var at
the same fake server and start a chat —
    ROCKYCODE_LSP_COMMAND="python tests/fake_lsp_server.py" rockycode chat
then read a file; rocky appends the (canned) diagnostics. Swap in a real
LSP-MCP wrapper (e.g. pyright behind one) to check against actual code.

Covers: connect + get_diagnostics formats output; the Phase-2 tools resolve and
return; and the hang-fix — a call after the server stops returns promptly
(bounded), never blocking the agent loop.
"""
import asyncio
import sys
from pathlib import Path

from rockycode.engine.lsp import LSPManager, build_lsp_tools

SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"


async def main():
    mgr = LSPManager(sys.executable, [str(SERVER)])
    await mgr.start()
    await asyncio.wait_for(mgr.ready, timeout=30)

    # auto-append path (what read_file uses to attach diagnostics)
    diag = await mgr.get_diagnostics("src/models.py")
    assert "LSP diagnostics" in diag and "Unused import" in diag, diag
    print("lsp get_diagnostics: formats server output  ✓")

    # Phase-2 explicit tools resolve against the server's advertised tool names
    tools = build_lsp_tools(mgr)
    out = await tools["lsp_lookup"].fn(symbol="User", action="definition")
    assert "class User" in out, out
    out = await tools["lsp_diagnostics"].fn(path="")
    assert "models.py" in out, out
    print("lsp tools: lookup + diagnostics return results  ✓")

    # hang-fix: after the server stops, a call returns '' promptly (<=10s), not a hang
    await mgr.stop()
    got = await asyncio.wait_for(mgr.get_diagnostics("x.py"), timeout=10)
    assert got == "", f"expected '' after stop, got {got!r}"
    print("lsp hang-fix: call after server stop returns '' fast, no hang  ✓")

    print("LSP SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
