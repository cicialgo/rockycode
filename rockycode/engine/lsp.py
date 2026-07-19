"""LSP bridge — connect to a language server via MCP and expose diagnostics.

Design: reuses the MCP actor pattern from mcp.py (one asyncio task per
connection, Queue-based dispatch, ready Future). The external MCP server
(e.g. karellen-lsp-mcp, zircote/lsp-tools) wraps an actual LSP server
(pyright / gopls / typescript-language-server) and exposes its operations
as MCP tools.

Phase 1: diagnostics-only.  Phase 2 adds the 4 semantic tools.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


class LSPManager:
    """Connect to an LSP MCP server and provide diagnostics.

    If *config* is None the manager is a no-op — diagnostics always returns
    an empty string.  This lets callers unconditionally append without
    checking whether LSP is active.
    """

    CALL_TIMEOUT_S = 30  # diagnostics on a cold LSP can take a moment

    def __init__(self, command: str, args: list[str] | None = None) -> None:
        self.command = command
        self.args = args or []
        self._queue: asyncio.Queue | None = None
        self.ready: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._tool_names: set[str] = set()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Launch the LSP MCP server subprocess and initialise the session."""
        self._queue = asyncio.Queue()
        self.ready = asyncio.get_event_loop().create_future()
        self._task = asyncio.create_task(self._run(), name="lsp-bridge")

    async def _run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        from rockycode.engine.mcp import _safe_child_env

        devnull = open(os.devnull, "w")
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=_safe_child_env({}),  # scrub secrets from the LSP subprocess too
        )
        try:
            async with stdio_client(params, errlog=devnull) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tool_names = {t.name for t in listed.tools}
                    if not self.ready.done():
                        self.ready.set_result(None)
                    while True:
                        req = await self._queue.get()
                        if req is None:
                            return
                        tool_name, arguments, fut = req
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(tool_name, arguments),
                                timeout=self.CALL_TIMEOUT_S,
                            )
                            if not fut.done():
                                fut.set_result(result)
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            # Worker is exiting (server died, or stop requested): fail every
            # still-queued request so its caller's `await fut` returns instead of
            # hanging forever. Pairs with the wait_for timeout in _call.
            devnull.close()
            if self._queue is not None:
                while not self._queue.empty():
                    try:
                        item = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is not None and not item[2].done():
                        item[2].set_exception(RuntimeError("lsp server stopped"))

    async def _call(self, tool_name: str, arguments: dict) -> str:
        # No live worker (never started, or the server process died) → don't
        # enqueue into a queue nothing will drain; that used to hang forever.
        if self._task is None or self._queue is None or self._task.done():
            return ""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((tool_name, arguments, fut))
        try:
            # Bound the wait: a worker that dies AFTER we enqueue, or a wedged
            # server, must never block the agent loop. A hair above the per-call
            # timeout inside _run so a genuinely slow call reports its own error.
            result = await asyncio.wait_for(fut, timeout=self.CALL_TIMEOUT_S + 5)
        except asyncio.TimeoutError:
            return ""
        # MCP CallToolResult — first text content
        for c in (result.content or []):
            if hasattr(c, "text"):
                return c.text
        return ""

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def get_diagnostics(self, path: str | Path) -> str:
        """Return formatted diagnostics for *path*, or '' if none / no LSP."""
        if self._task is None:
            return ""
        try:
            raw = await self._call("diagnostics", {"path": str(path)})
        except Exception:  # noqa: BLE001
            return ""
        if not raw or not raw.strip():
            return ""
        lines = raw.strip().splitlines()
        if not lines:
            return ""
        header = "\n  LSP diagnostics\n  " + "-" * 39
        body = "\n  ".join(lines[:20])  # cap at 20 diag lines
        if len(lines) > 20:
            body += f"\n  ... and {len(lines) - 20} more"
        return header + "\n  " + body

    async def stop(self) -> None:
        if self._task is None or self._queue is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # tool names exposed by the LSP MCP server (for Phase 2)
    # ------------------------------------------------------------------

    @property
    def available_tools(self) -> set[str]:
        return self._tool_names


# ------------------------------------------------------------------
# resolve an LSP MCP server config from the environment
# ------------------------------------------------------------------

def resolve_lsp_config() -> tuple[str, list[str]] | None:
    """Return (command, args) for an LSP MCP server, or None.

    Checks, in order:
      1. ROCKYCODE_LSP_COMMAND env var (shell-split on whitespace)
      2. Auto-detection: pyright --version → karellen-lsp-mcp-like wrapper
    """
    env_cmd = os.getenv("ROCKYCODE_LSP_COMMAND")
    if env_cmd:
        parts = env_cmd.split()
        return parts[0], parts[1:]
    return None


# ------------------------------------------------------------------
# Phase 2 — four composite LSP tools
# ------------------------------------------------------------------

# Map our semantic actions to likely MCP tool names on the LSP server.
# Multiple variants are tried in order — the first available one wins.
_ACTION_TOOL_MAP: dict[str, list[str]] = {
    "definition":     ["definition", "go_to_definition", "goToDefinition"],
    "references":     ["references", "find_references", "findReferences"],
    "hover":          ["hover", "hover_info", "hoverInfo"],
    "implementation": ["implementation", "go_to_implementation", "goToImplementation"],
    "callers":        ["callers", "incoming_calls", "incomingCalls", "call_hierarchy_incoming"],
    "callees":        ["callees", "outgoing_calls", "outgoingCalls", "call_hierarchy_outgoing"],
}


def build_lsp_tools(lsp: "LSPManager") -> dict:
    """Return 4 composite LSP tools bound to *lsp*.

    Each tool produces human-readable text, not raw JSON — designed for
    DeepSeek's reasoning_content to digest efficiently.
    """
    from rockycode.engine.tools import Tool, _fn_schema, _truncate

    def _pick_tool(actions: list[str]) -> str | None:
        """Return the first MCP tool name from *actions* that the server supports."""
        for name in actions:
            if name in lsp.available_tools:
                return name
        return None

    async def _call_or_fallback(tool_names: list[str], arguments: dict) -> str:
        tool = _pick_tool(tool_names)
        if tool is None:
            return f"[error] lsp server does not support this operation (tried: {', '.join(tool_names[:3])})"
        try:
            raw = await lsp._call(tool, arguments)
        except Exception as exc:  # noqa: BLE001
            return f"[error] lsp call failed: {type(exc).__name__}: {exc}"
        if not raw or not raw.strip():
            return "[no results]"
        return raw.strip()

    # -- lsp_lookup -------------------------------------------------------

    _LOOKUP_ACTIONS = ["definition", "references", "hover", "implementation", "callers", "callees"]

    async def lsp_lookup(symbol: str, action: str) -> str:
        """Look up a symbol — definition, references, hover info, etc."""
        action = action.lower().strip()
        if action not in _LOOKUP_ACTIONS:
            return f"[error] unknown action '{action}'. choose: {', '.join(_LOOKUP_ACTIONS)}"
        tool_names = _ACTION_TOOL_MAP[action]
        result = await _call_or_fallback(tool_names, {"symbol": symbol})
        header = f"lsp {action}: {symbol}\n" + "-" * (len(action) + len(symbol) + 5)
        return _truncate(f"{header}\n{result}", 8000)

    # -- lsp_symbol_search -------------------------------------------------

    async def lsp_symbol_search(query: str) -> str:
        """Search for symbols across the project by name (fuzzy)."""
        result = await _call_or_fallback(
            ["workspace_symbol", "workspaceSymbol", "symbol_search", "search_symbols"],
            {"query": query},
        )
        header = f"symbol search: {query}\n" + "-" * (len(query) + 15)
        return _truncate(f"{header}\n{result}", 8000)

    # -- lsp_file_symbols --------------------------------------------------

    async def lsp_file_symbols(path: str) -> str:
        """List all symbols in a file (classes, functions, etc.) with signatures."""
        result = await _call_or_fallback(
            ["document_symbol", "documentSymbol", "file_symbols"],
            {"path": path},
        )
        header = f"symbols in {path}\n" + "-" * (len(path) + 12)
        return _truncate(f"{header}\n{result}", 8000)

    # -- lsp_diagnostics (active query) ------------------------------------

    async def lsp_diagnostics(path: str = "") -> str:
        """Get diagnostics for a file, or project-wide if path is empty."""
        args = {"path": path} if path else {}
        result = await _call_or_fallback(
            ["diagnostics", "get_diagnostics", "publish_diagnostics"],
            args,
        )
        label = path or "project"
        header = f"diagnostics: {label}\n" + "-" * (len(label) + 14)
        return _truncate(f"{header}\n{result}", 8000)

    # -- schemas -----------------------------------------------------------

    schemas = {
        "lsp_lookup": _fn_schema(
            "lsp_lookup",
            "Look up a symbol using LSP code intelligence — get its definition, "
            "all references, type signature (hover), interface implementations, "
            "or who calls / is called by it. One call replaces several grep "
            "searches with precise, compiler-accurate results.",
            {
                "symbol": {
                    "type": "string",
                    "description": "Fully-qualified or plain symbol name, e.g. 'UserModel.get_name' or 'get_name'.",
                },
                "action": {
                    "type": "string",
                    "enum": _LOOKUP_ACTIONS,
                    "description": "What to look up: definition, references, hover, implementation, callers, callees.",
                },
            },
            ["symbol", "action"],
        ),
        "lsp_symbol_search": _fn_schema(
            "lsp_symbol_search",
            "Search for symbols across the entire project by name. "
            "Returns matching classes, functions, interfaces — fuzzy, not regex. "
            "Use this to find where something is defined when you don't know the file.",
            {
                "query": {
                    "type": "string",
                    "description": "Partial symbol name to search for, e.g. 'UserAuth' or 'handle_login'.",
                },
            },
            ["query"],
        ),
        "lsp_file_symbols": _fn_schema(
            "lsp_file_symbols",
            "List all symbols (classes, functions, methods, variables) in a file "
            "with their type signatures. Use to quickly understand a file's "
            "structure without reading every line.",
            {
                "path": {
                    "type": "string",
                    "description": "Path to the file to inspect.",
                },
            },
            ["path"],
        ),
        "lsp_diagnostics": _fn_schema(
            "lsp_diagnostics",
            "Get compiler/linter diagnostics for a file, or project-wide if no "
            "path is given. Returns errors, warnings, and hints with line numbers. "
            "Use after making changes to verify correctness.",
            {
                "path": {
                    "type": "string",
                    "description": "File path to check. Omit for project-wide diagnostics.",
                },
            },
            [],  # path is optional
        ),
    }

    fns = {
        "lsp_lookup": lsp_lookup,
        "lsp_symbol_search": lsp_symbol_search,
        "lsp_file_symbols": lsp_file_symbols,
        "lsp_diagnostics": lsp_diagnostics,
    }
    return {name: Tool(name=name, schema=schemas[name], fn=fns[name]) for name in fns}


# ------------------------------------------------------------------
# Phase 4 — multi-tenant LSP manager
# ------------------------------------------------------------------

class MultiTenantLSPManager:
    """Lazy per-session LSP connections with idle timeout.

    Each session (sandbox / worktree / bench task) gets its own LSPManager
    instance, keyed by a string session_id.  Sessions are created on first
    access and destroyed after *idle_timeout* seconds of inactivity.

    In single-session mode the key ``"default"`` is used automatically,
    making MultiTenantLSPManager a drop-in replacement for LSPManager.
    """

    IDLE_TIMEOUT = 300  # 5 minutes

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        idle_timeout: int = IDLE_TIMEOUT,
    ) -> None:
        self._command = command
        self._args = args or []
        self._idle_timeout = idle_timeout
        self._sessions: dict[str, LSPManager] = {}
        self._last_used: dict[str, float] = {}
        self._cleanup_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self, session_id: str = "default") -> None:
        """Ensure a session is started.  Safe to call multiple times."""
        import time as _time
        if session_id not in self._sessions:
            mgr = LSPManager(self._command, list(self._args))
            await mgr.start()
            self._sessions[session_id] = mgr
        self._last_used[session_id] = _time.monotonic()

    async def start_cleanup_loop(self) -> None:
        """Begin background cleanup of idle sessions.  Call once."""
        if self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="lsp-cleanup")

    async def _cleanup_loop(self) -> None:
        import time as _time
        while True:
            await asyncio.sleep(min(self._idle_timeout // 2, 60))
            now = _time.monotonic()
            stale = [
                sid for sid, last in self._last_used.items()
                if sid != "default" and now - last > self._idle_timeout
            ]
            for sid in stale:
                mgr = self._sessions.pop(sid, None)
                self._last_used.pop(sid, None)
                if mgr is not None:
                    try:
                        await mgr.stop()
                    except Exception:  # noqa: BLE001
                        pass

    # -- per-session ---------------------------------------------------------

    async def get_or_create(self, session_id: str = "default") -> LSPManager:
        """Return the LSPManager for *session_id*, creating it if needed."""
        import time as _time
        if session_id not in self._sessions:
            await self.start(session_id)
        self._last_used[session_id] = _time.monotonic()
        return self._sessions[session_id]

    async def evict(self, session_id: str) -> None:
        """Stop and remove a session (e.g. sandbox destroyed)."""
        mgr = self._sessions.pop(session_id, None)
        self._last_used.pop(session_id, None)
        if mgr is not None:
            await mgr.stop()

    # -- delegation (for the default session) --------------------------------

    async def get_diagnostics(self, path: str | Path) -> str:
        """Get diagnostics from the default session (no auto-create)."""
        mgr = self._sessions.get("default")
        if mgr is None:
            return ""
        try:
            return await mgr.get_diagnostics(path)
        except Exception:  # noqa: BLE001
            return ""

    @property
    def is_running(self) -> bool:
        mgr = self._sessions.get("default")
        return mgr is not None and mgr.is_running

    @property
    def available_tools(self) -> set[str]:
        mgr = self._sessions.get("default")
        return mgr.available_tools if mgr else set()

    @property
    def command(self) -> str:
        return self._command

    async def _call(self, tool_name: str, arguments: dict) -> str:
        mgr = self._sessions.get("default")
        if mgr is None:
            return "[error] lsp not connected — start a session first"
        return await mgr._call(tool_name, arguments)

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        for mgr in self._sessions.values():
            try:
                await mgr.stop()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        self._last_used.clear()

    @property
    def ready(self) -> "asyncio.Future | None":
        """Expose the default session's ready Future for compatibility."""
        mgr = self._sessions.get("default")
        return mgr.ready if mgr else None
