"""LSP bridge (experimental) — code navigation through an LSP-over-MCP server.

Rocky does not speak the language-server protocol itself. It speaks MCP to an
external wrapper the user names in ROCKYCODE_LSP_COMMAND, and that wrapper
drives the real server (pyright / gopls / typescript-language-server). The
tool names and argument shapes below are assumptions about that wrapper; the
only server they are tested against is tests/fake_lsp_server.py. Nothing is
auto-detected, and no wrapper is endorsed.

What this module promises is honesty about the result: a query that failed
says so (`[error]` / `[timeout]` first, so tools.execute counts it as a
failure), and `[no results]` only ever means a query that worked and matched
nothing. Lint and type checks after an edit are engine/checks.py's job
(`check_code`), which needs no server; nothing here rides on read_file.

Design: reuses the MCP actor pattern from mcp.py (one asyncio task per
connection, Queue-based dispatch, ready Future).
"""
from __future__ import annotations

import asyncio
import os
import shlex


class LSPUnavailable(RuntimeError):
    """There is no live server behind the bridge: never started, or it exited."""


class LSPError(RuntimeError):
    """The server answered the call with an error."""


class LSPManager:
    """One connection to an LSP-over-MCP server. `_call` returns the server's
    text, or raises: LSPUnavailable (no live server), TimeoutError (no answer
    in time), LSPError (the server reported an error)."""

    CALL_TIMEOUT_S = 30  # a cold language server can take a moment

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
                        item[2].set_exception(LSPUnavailable("the lsp server stopped"))

    async def _call(self, tool_name: str, arguments: dict) -> str:
        # No live worker (never started, or the server process died) → don't
        # enqueue into a queue nothing will drain; that used to hang forever.
        # And say so: an empty string here used to read as "no results".
        if self._task is None or self._queue is None or self._task.done():
            raise LSPUnavailable("the lsp server is not running")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((tool_name, arguments, fut))
        # Bound the wait: a worker that dies AFTER we enqueue, or a wedged
        # server, must never block the agent loop. A hair above the per-call
        # timeout inside _run so a genuinely slow call reports its own error.
        # Either way the caller gets a TimeoutError, never an empty answer.
        result = await asyncio.wait_for(fut, timeout=self.CALL_TIMEOUT_S + 5)
        # MCP CallToolResult: every text block counts, and isError is an error.
        text = "\n".join(c.text for c in (result.content or []) if getattr(c, "text", None))
        if getattr(result, "isError", False):
            raise LSPError(text.strip() or "the server reported an error")
        return text

    async def stop(self) -> None:
        if self._task is None or self._queue is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

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
    """(command, args) from ROCKYCODE_LSP_COMMAND, or None when it is not set.
    Nothing is auto-detected. The value is a command line, so a quoted path
    with spaces stays one argument; unbalanced quotes raise ValueError rather
    than launch something else. It is run directly, never through a shell."""
    raw = (os.getenv("ROCKYCODE_LSP_COMMAND") or "").strip()
    if not raw:
        return None
    parts = shlex.split(raw, posix=os.name != "nt")
    return parts[0], parts[1:]


# ------------------------------------------------------------------
# the four LSP tools
# ------------------------------------------------------------------

# Map our semantic actions to likely MCP tool names on the LSP server.
# Multiple variants are tried in order — the first available one wins.
# These are guesses about the wrapper, not a contract (see the module docstring).
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

    Each tool produces human-readable text, not raw JSON. A failed query
    begins with `[error]` or `[timeout]`; a header never sits in front of
    one, because tools.execute reads the first characters to decide whether
    the call worked.
    """
    from rockycode.engine.tools import Tool, _fn_schema, _truncate

    async def _query(what: str, tool_names: list[str], arguments: dict) -> str:
        """Run one query and render it. Five outcomes, five different texts:
        not running, unsupported by this server, timed out, server error —
        all failures — and a result, which may be `[no results]`."""
        if not lsp.is_running:
            return f"[error] {what}: the lsp server is not running (/lsp shows its state; grep and read_file still work)"
        tool = next((name for name in tool_names if name in lsp.available_tools), None)
        if tool is None:
            return (f"[error] {what}: this lsp server has no such operation "
                    f"(looked for: {', '.join(tool_names[:3])})")
        try:
            raw = await lsp._call(tool, arguments)
        except LSPUnavailable as exc:
            return f"[error] {what}: {exc}"
        except TimeoutError:
            return f"[timeout] {what}: the lsp server did not answer in time"
        except Exception as exc:  # noqa: BLE001 — a server error must reach the model as one
            return f"[error] {what}: {type(exc).__name__}: {exc}"
        return _truncate(f"{what}\n{'-' * len(what)}\n{raw.strip() or '[no results]'}", 8000)

    # -- lsp_lookup -------------------------------------------------------

    _LOOKUP_ACTIONS = ["definition", "references", "hover", "implementation", "callers", "callees"]

    async def lsp_lookup(symbol: str, action: str) -> str:
        """Look up a symbol — definition, references, hover info, etc."""
        action = action.lower().strip()
        if action not in _LOOKUP_ACTIONS:
            return f"[error] unknown action '{action}'. choose: {', '.join(_LOOKUP_ACTIONS)}"
        return await _query(f"lsp {action}: {symbol}", _ACTION_TOOL_MAP[action], {"symbol": symbol})

    # -- lsp_symbol_search -------------------------------------------------

    async def lsp_symbol_search(query: str) -> str:
        """Search for symbols across the project by name (fuzzy)."""
        return await _query(f"symbol search: {query}",
                            ["workspace_symbol", "workspaceSymbol", "symbol_search", "search_symbols"],
                            {"query": query})

    # -- lsp_file_symbols --------------------------------------------------

    async def lsp_file_symbols(path: str) -> str:
        """List all symbols in a file (classes, functions, etc.) with signatures."""
        return await _query(f"symbols in {path}", ["document_symbol", "documentSymbol", "file_symbols"],
                            {"path": path})

    # -- lsp_diagnostics (an explicit query; nothing runs it for you) ------

    async def lsp_diagnostics(path: str = "") -> str:
        """Get diagnostics for a file, or project-wide if path is empty."""
        return await _query(f"diagnostics: {path or 'project'}",
                            ["diagnostics", "get_diagnostics", "publish_diagnostics"],
                            {"path": path} if path else {})

    # -- schemas -----------------------------------------------------------

    schemas = {
        "lsp_lookup": _fn_schema(
            "lsp_lookup",
            "Look up a symbol through the connected language server — its definition, "
            "all references, type signature (hover), interface implementations, "
            "or who calls / is called by it. One call can replace several grep "
            "searches. A result starting with [error] or [timeout] is a failed "
            "query, not an empty one: fall back to grep.",
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
            "Ask the connected language server for diagnostics on a file, or "
            "project-wide if no path is given: errors, warnings and hints with line "
            "numbers. Nothing runs this for you after an edit. For lint and type "
            "checks without a language server, use check_code.",
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
# multi-session manager
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

    @property
    def is_running(self) -> bool:
        mgr = self._sessions.get("default")
        return mgr is not None and mgr.is_running

    @property
    def available_tools(self) -> set[str]:
        mgr = self._sessions.get("default")
        return mgr.available_tools if mgr else set()

    @property
    def args(self) -> list[str]:
        return list(self._args)

    @property
    def command(self) -> str:
        return self._command

    async def _call(self, tool_name: str, arguments: dict) -> str:
        mgr = self._sessions.get("default")
        if mgr is None:
            raise LSPUnavailable("the lsp server is not running")
        return await mgr._call(tool_name, arguments)

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
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
