"""Interactive tool composition shared by CLI and desktop.

Engine owns the agent loop. Runtime owns feature assembly and external resource
lifetimes. A host supplies approvals, presents Engine events, stops its active
turns before closing Runtime, and opts into loops only if it has a tick scheduler.
Benchmark/headless exec profiles deliberately retain their separate contracts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from rockycode.engine.loop import Engine
from rockycode.engine.trajectory import TrajectoryLogger


class Runtime:
    def __init__(self, engine, config, *, sandbox=None, startup_approver=None, isolated=False):
        self.engine, self.config = engine, config
        self.isolated = isolated
        self.sandbox, self.startup_approver = sandbox, startup_approver
        self.features, self.notices, self.cleanup_errors = {}, [], []
        self._prompt = engine._base_system
        self._mcp_lock, self._lsp_lock = asyncio.Lock(), asyncio.Lock()
        self._mcp_started = self._lsp_started = self._closed = False
        self._close_task = None
        self._starting = set()
        self.lsp_error = None

    def refresh_prompt(self):
        if self.isolated:
            # A private host supplies the complete contract. No ambient files,
            # home paths, language/mode preferences, or environment stamps.
            self.engine.set_base_system(self._prompt)
            return
        from rockycode.prompts.rocky import tools_section, with_environment, with_language, with_today
        text = with_environment(self._prompt + tools_section(self.engine.registry), self.engine.workdir)
        self.engine.set_base_system(with_today(with_language(text, self.config["language"])))

    def publish_registry(self, tools, *, reason):
        """The one place a running Runtime adds tools to the catalog
        (docs/design/call-identity.md §3.2). Advances the generation the Engine
        stamps on every request and receipt, records why, and re-renders the
        tools section of the prompt. Construction is generation 0; every change
        after it counts, a sandbox swap at build time included. Phase 1 counts;
        binding a request to a snapshot is phase 2."""
        self.engine.registry.update(tools)
        generation = self.engine.bump_registry(reason, tools=sorted(tools))
        self.refresh_prompt()
        return generation

    async def _approve_start(self, name, args):
        # User-configured CLI processes retain existing startup behavior.
        return self.startup_approver is None or await self.startup_approver(name, args)

    async def start_mcp(self):
        task = asyncio.current_task()
        self._starting.add(task)
        try:
            await self._start_mcp()
        finally:
            self._starting.discard(task)

    async def _start_mcp(self):
        async with self._mcp_lock:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            manager = self.engine.mcp_manager
            if self._mcp_started or manager is None:
                return
            allowed = {}
            for name, config in manager.configs.items():
                if await self._approve_start("runtime.start_mcp", {
                    "server": name, "command": config.command, "args": config.args,
                    "source": config.source,
                    # Bind remembered startup consent to explicit environment changes
                    # without sending credentials to a host UI or its stored records.
                    "configFingerprint": hashlib.sha256(
                        json.dumps(config.env, sort_keys=True).encode()).hexdigest(),
                }):
                    allowed[name] = config
                else:
                    manager.failures[name] = "startup denied by user"
            manager.configs = allowed  # never rewrite the user's config file
            await manager.start()
            self.publish_registry(manager.tools(), reason="mcp")
            self._mcp_started = True

    async def start_lsp(self):
        task = asyncio.current_task()
        self._starting.add(task)
        try:
            await self._start_lsp()
        finally:
            self._starting.discard(task)

    async def _start_lsp(self):
        async with self._lsp_lock:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            manager = self.engine.lsp_manager
            if self._lsp_started or manager is None:
                return
            if not await self._approve_start("runtime.start_lsp", {
                "command": manager.command, "args": manager.args,
            }):
                self.lsp_error = "startup denied by user"
                self._lsp_started = True
                return
            try:
                await manager.start()
                await asyncio.wait_for(asyncio.shield(manager.ready), timeout=30)
                await manager.start_cleanup_loop()
                from rockycode.engine.lsp import build_lsp_tools
                self.publish_registry(build_lsp_tools(manager), reason="lsp")
            except asyncio.CancelledError:
                await manager.stop()
                raise
            except Exception as exc:
                self.lsp_error = str(exc)
                await manager.stop()
            self._lsp_started = True

    async def start(self):
        # Sequential startup keeps approval cards and their responses unambiguous.
        await self.start_mcp()
        await self.start_lsp()

    def describe(self):
        """Report registration separately from external-service availability."""
        from rockycode.engine.redact import redact
        from rockycode.i18n import tr
        engine, caps = self.engine, []

        def add(name, status, detail="", *, key=None, **params):
            # `detail` is the English text (compat); clients that localize use
            # `detail_key` + `params` with their own catalog (desktop) or ours (TUI).
            text = tr(key, "en", **params) if key else detail
            caps.append({"name": name, "status": status, "detail": redact(text),
                         "detail_key": key, "params": params})

        add("core", "ready", key="capability_detail.core")
        add("skills", "ready" if self.features["skills"] else "disabled",
            key="capability_detail.skills", count=len(engine.skills))
        add("memory", "ready" if self.features["memory"] else "disabled", key="capability_detail.memory")
        index = engine.memory_index
        add("memory_index", "registered" if index and index.available else "disabled",
            key="capability_detail.memory_index")
        add("web", "registered" if self.features["web"] else "disabled",
            key="capability_detail.web")
        manager = engine.mcp_manager
        if manager is None:
            add("mcp", "disabled")
        elif manager.actors:
            add("mcp", "partial" if manager.failures or manager.untrusted else "ready",
                key="capability_detail.mcp_running", servers=len(manager.actors), tools=len(manager.tools()),
                failed=len(manager.failures), untrusted=len(manager.untrusted))
        elif manager.failures:
            add("mcp", "failed", "; ".join(manager.status()))
        elif manager.configs:
            add("mcp", "pending", key="capability_detail.mcp_pending")
        elif manager.untrusted:
            add("mcp", "disabled", key="capability_detail.mcp_untrusted")
        else:
            add("mcp", "not_configured", "; ".join(manager.notices),
                key=None if manager.notices else "capability_detail.mcp_none")
        add("lsp", "failed" if self.lsp_error else "ready" if self._lsp_started else
            "pending" if engine.lsp_manager else
            "not_configured" if self.features["lsp"] else "disabled", self.lsp_error or "")
        add("artifacts", "registered",
            key="capability_detail.artifacts_desktop" if getattr(self.engine, "artifact_opener", None) is not None
            else "capability_detail.artifacts")
        add("goal_review", "registered", key="capability_detail.goal_review")
        add("explore", "registered" if "explore" in engine.registry else "disabled")
        add("loops", "registered" if self.features["loops"] else "disabled",
            key="capability_detail.loops")
        return {"tools": sorted(engine.registry), "capabilities": caps}

    async def close(self):
        # A caller timeout must not cancel half-completed cleanup.
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        starting = list(self._starting)
        for task in starting:
            task.cancel()
        await asyncio.gather(*starting, return_exceptions=True)
        async def cleanup(name, fn):
            try:
                await asyncio.wait_for(fn(), timeout=8)
            except Exception as exc:
                from rockycode.engine.redact import redact
                self.cleanup_errors.append(f"{name}: {redact(str(exc))}")

        jobs = []
        for name in ("mcp_manager", "lsp_manager", "artifact_server"):
            resource = getattr(self.engine, name, None)
            if resource is not None:
                jobs.append(cleanup(name, resource.stop))
        if self.sandbox is not None and self.sandbox.is_running:
            jobs.append(cleanup("sandbox", self.sandbox.stop))
        if self.engine.memory_index is not None:
            jobs.append(cleanup("memory_index", self.engine.memory_index.close))
        close = getattr(self.engine.client, "close", None)
        if close is not None:
            jobs.append(cleanup("model_client", close))
        try:
            self.engine.finalize_outcome()
            # The graded final reflect lands after the heuristic record (readers
            # take the last outcome). Chat already ran it in the exit flow — this
            # is idempotent — so desktop/serve-style hosts get theirs here, bounded
            # so a host's close deadline is never blown by one slow model call.
            await self.engine.run_final_hooks(timeout=6.0)
        finally:
            await asyncio.gather(*jobs)


def build_runtime(
    *, model: str, workdir: Path, source: str = "chat", approver=None,
    startup_approver=None, client=None, allowed_roots=(), system_prompt=None,
    prompt_name=None, prompt_sha=None, thinking=True, reasoning_effort="max",
    max_tokens=0, context_window=0, max_steps=0, mcp=True, skills=True,
    memory=True, web=True, lsp=True, loops=False, live=False, sandbox=None,
    include_user=True, planning=True, isolated=False,
) -> Runtime:
    """Construct without launching processes; await start() on the host loop.

    CLI may start MCP/LSP individually to display progress. include_user=False
    skips user Skills/MCP discovery (isolated tests/cloud); bundled/project
    Skills remain available. Project .env never controls model credentials.

    isolated=True is a host-owned, tool-empty profile: explicit client and system
    prompt required, no ambient config/project notes or global persistence, and
    all optional features disabled. The host may install a restricted registry.
    """
    from rockycode.config import DEFAULTS, load as load_config
    from rockycode.session import Project, get_project
    from rockycode.prompts.rocky import ROCKY_SYSTEM, ROCKY_SYSTEM_ZH
    from rockycode.onboarding import bootstrap_credentials

    if isolated:
        if client is None or system_prompt is None or allowed_roots or sandbox is not None:
            raise ValueError("Isolated runtime requires an explicit client/prompt and no external roots/sandbox")
        mcp = skills = memory = web = lsp = loops = planning = include_user = False
    elif client is None:
        bootstrap_credentials()
    wd = Path(workdir).resolve()
    cfg = {**DEFAULTS, "evolve": "off", "mode": ""} if isolated else load_config(wd)
    project = Project(id="isolated", name="isolated", root=wd) if isolated else get_project(wd)
    if system_prompt is None:
        system_prompt = ROCKY_SYSTEM_ZH if cfg["language"] == "zh" else ROCKY_SYSTEM
        prompt_name = "rocky-builtin-zh" if cfg["language"] == "zh" else "rocky-builtin"
    prompt_name = prompt_name or "custom"
    prompt_sha = prompt_sha or hashlib.sha256(system_prompt.encode()).hexdigest()[:8]

    from rockycode.pricing import UsageLedger
    ledger = UsageLedger()

    # Project instructions: same files Claude Code / Codex users already have.
    notes_loaded = []
    for fname in (() if isolated else ("CLAUDE.md", "AGENTS.md")):
        p = wd / fname
        if p.exists():
            system_prompt += f"\n\n# Project instructions (from {fname})\n\n{p.read_text()[:20000]}"
            notes_loaded.append(fname)

    skill_list = []
    if skills:
        from rockycode.engine.skills import discover_skills, skills_prompt_section
        skill_list = discover_skills(wd, home=Path.home() if include_user else None)
        if skill_list:
            system_prompt += skills_prompt_section(skill_list)

    mem_store = None
    mem_loaded: list[str] = []
    if memory:
        from rockycode.memory import MemoryStore, memory_prompt_section
        mem_store = MemoryStore.for_workdir(wd)
        section = memory_prompt_section(mem_store)
        if section:
            system_prompt += section
            mem_loaded = [m.name for m in mem_store.load_all() if m.status == "active"]

    # NOTE: not final — tools/language/environment/date are appended after all
    # tool registration below (engine.set_base_system), before the first turn.
    engine = Engine(
        model=model, client=client, approver=approver,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        # flag/env → config → the model's own registry limits (None)
        max_tokens=max_tokens or cfg["max_tokens"] or None,
        context_window=context_window or cfg["context_window"] or None,
        max_steps=max_steps,
        workdir=wd,
        allowed_roots=allowed_roots,
        system_prompt=system_prompt,
        registry={} if isolated else None,
        # Isolated host owns persistence: a disabled logger, nothing lands in ~/.rockycode.
        trajectory_logger=TrajectoryLogger({}, enabled=False) if isolated else None,
        trajectory_meta={
            "source": source,
            "project_id": project.id,
            "project_name": project.name,
            "prompt_name": prompt_name,
            "prompt_sha": prompt_sha,
            "project_notes": notes_loaded,
            "skills": [s.name for s in skill_list],
            "memories": mem_loaded,
            "allowed_roots": [str(r) for r in allowed_roots],
        },
    )

    # Attached as plain attributes (not Engine params) to keep loop.py
    # untouched — MCP/skills/notes are chat-session concerns, not engine concerns.
    engine.ledger = ledger
    engine.project_notes = notes_loaded
    engine.skills = skill_list
    if skill_list:
        from rockycode.engine.skills import build_skill_tool
        tool = build_skill_tool(skill_list)
        engine.registry[tool.name] = tool
    # rocky configures rocky: a tool that edits rocky's OWN settings (config
    # keys, own base URLs, custom providers) under ~/.rockycode — ask-tier,
    # never a key. Pairs with the built-in `rocky-setup` skill.
    if not isolated:
        from rockycode.engine.selfconfig import build_selfconfig_tool
        _sc = build_selfconfig_tool(wd)
        engine.registry[_sc.name] = _sc
    engine.memory_store = mem_store
    engine.memory_index = None
    if mem_store is not None:
        from rockycode.memory import build_memory_tools
        from rockycode.memory.index import IndexUnavailable, MemoryIndex
        try:
            mem_index = MemoryIndex(mem_store)
            engine.memory_index = mem_index
            mem_index.conn()  # probe sqlite-vec now; fall back loudly, not mid-chat
        except IndexUnavailable:
            mem_index = None
        for tool in build_memory_tools(mem_store, index=mem_index):
            engine.registry[tool.name] = tool
    # Web tools are chat-only: bench stays offline + uncontaminated.
    web_tools: dict = {}
    engine.web_enabled = web
    if web:
        from rockycode.engine.web import build_web_tools, default_search_order
        # Pass the session ledger so web_search/research flash-model tokens count
        # toward the displayed cost (were silently uncounted).
        web_tools = build_web_tools(ledger=ledger)
        for tool in web_tools.values():
            engine.registry[tool.name] = tool
        engine.web_order = default_search_order()

    engine.mcp_manager = None
    if mcp:
        from rockycode.engine.mcp import MCPManager, discover
        servers, notices = discover(wd, include_user=include_user)
        engine.mcp_manager = MCPManager(servers, notices)

    # LSP — config resolved here; connection is started async in the TUI
    # (same two-phase pattern as MCP: construct here, start in on_mount).
    engine.lsp_enabled = lsp
    engine.lsp_manager = None
    lsp_config_error = None
    if lsp:
        from rockycode.engine.lsp import MultiTenantLSPManager, resolve_lsp_config
        try:
            lsp_cfg = resolve_lsp_config()
        except ValueError as exc:  # unbalanced quotes: say so, start nothing
            lsp_cfg, lsp_config_error = None, f"ROCKYCODE_LSP_COMMAND is not a command line ({exc})"
        if lsp_cfg:
            cmd, args = lsp_cfg
            engine.lsp_manager = MultiTenantLSPManager(cmd, args)
        # LSP is opt-in; when it's not configured (the default) stay quiet rather
        # than nag every startup. `/lsp` shows status and how to set it up.

    # Artifact live-mode state. The create_artifact tool itself is registered
    # after any sandbox swap (below) so it stays a host tool. Static file:// by
    # default; --live or the TUI live action opts into local auto-refresh.
    engine.artifact_live = bool(live)  # TUI saves first; preview is explicit
    engine.artifact_server = None

    # Interactive planning (/plan): submit_plan + the host-owned PlanningSession.
    # Registered here, before the sandbox swap and prompt finalization, so every
    # build_runtime host shares one tool set; headless/bench never come through
    # here and keep their tool sets unchanged.
    if planning:
        engine.enable_planning()

    sb = sandbox
    if sb is not None:
        from rockycode.engine.sandbox import build_sandbox_registry
        engine.swap_registry(build_sandbox_registry(sb, extras=web_tools))

    # read_file stays read_file whether or not LSP is on. Diagnostics used to
    # ride on every read: each one waited on the language server, a refused read
    # still asked it about the path, and a dead server looked like clean code.
    # After an edit, `check_code` runs the linters with no server; an explicit
    # `lsp_diagnostics` call is there for whoever wants the server's view.

    # Artifact tool — host tool (writes to host fs, opens host browser); added
    # after any sandbox swap so it survives there too.
    if not isolated:
        from rockycode.engine.artifact import build_artifact_tools
        for tool in build_artifact_tools(workdir=wd, engine=engine).values():
            engine.registry[tool.name] = tool

    # Goal review/merge — host tools that act on the real repo (git), so a /goal
    # branch can be reviewed and safely merged from chat. Host-side, so they run
    # against the origin repo even when the chat tools are sandboxed. The reviewer
    # makes review_goal_branch buy a grounded, citation-checked review from a
    # read-only explore child (reads the branch via git refs — host-side, so this
    # holds in sandbox mode too) instead of dumping a 40k-char raw diff into chat.
    if not isolated:
        from rockycode.engine.explore import make_branch_reviewer
        from rockycode.goal.review import build_goal_tools
        for tool in build_goal_tools(workdir=wd, reviewer=make_branch_reviewer(engine)).values():
            engine.registry[tool.name] = tool

    # explore — buy a read-only, citation-verified investigation instead of
    # grepping it into this context (engine/explore.py). Skipped IN sandbox
    # mode: children read the HOST tree, which would quietly bypass the
    # container the user asked for; sandbox-aware children are a follow-up.
    if sb is None and not isolated:
        from rockycode.engine.explore import build_explore_tool
        engine.registry.update(build_explore_tool(engine))

    # Loops (/loop — engine/cron.py): loop_start / loop_stop let rocky set up a
    # check-in from one sentence ("check every 10 minutes whether it finished").
    # Session state, not files — host tools, registered after any sandbox swap
    # so they survive it; the TUI attaches its receipt hook at mount.
    engine.loop_book = None
    if loops:
        from rockycode.engine.cron import LoopBook, build_loop_tools
        engine.loop_book = LoopBook(currency=cfg["currency"])
        engine.registry.update(build_loop_tools(engine.loop_book))

    # Finalize the system prompt now that the registry is complete: the tools
    # section is GENERATED from what actually registered (the old hand-written
    # sentence advertised web/artifact tools in bench and under --no-web where
    # they never existed), then language (config auto|en|zh — resolved once,
    # prefix stays byte-stable), one environment line, and the date stamp.
    # Order puts the zh 语言要求 block near the end: recency beats the English
    # sections above it. Must run BEFORE the launch-mode swap below, so the
    # mode contract layers on the final base.
    # Evolve v2: reflection checkpoints on the session's own model (default on;
    # "off" kills every evolve call). Bench never comes through build_runtime,
    # so scores keep measuring the harness.
    if cfg.get("evolve", "off") == "auto":
        from rockycode.dream.reflect import attach as _attach_reflect
        _attach_reflect(engine)

    runtime = Runtime(engine, cfg, sandbox=sandbox, startup_approver=startup_approver, isolated=isolated)
    runtime.features = dict(skills=skills, memory=memory, web=web, mcp=mcp, lsp=lsp, loops=loops)
    runtime.lsp_error = lsp_config_error
    runtime.refresh_prompt()
    engine.runtime = runtime

    # Folder-default collaboration mode (config `mode`, set by `/research
    # always`). Built-ins only — a cloned repo's project-local mode file must
    # never auto-inject prompt text (see modes.py). Applied before the first
    # API call, so the prompt swap costs nothing cache-wise.
    if cfg.get("mode"):
        from rockycode.engine.modes import find_builtin
        _mode = find_builtin(str(cfg["mode"]))
        if _mode is not None:
            engine.set_mode(_mode.name, _mode.body)
        else:
            runtime.notices.append(f"config mode '{cfg['mode']}' is not a built-in mode — ignored.")

    return runtime
