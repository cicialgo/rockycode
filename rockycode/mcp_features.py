"""Host-facing Rocky capabilities. No UI, implicit grants, or nested MCP startup.

All long operations share one lane: Engine/Goal/Dream use process-global stores.
Jobs belong to this server process, not to a durable remote scheduler.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from mcp.types import ToolAnnotations

from rockycode.headless import _scrub, _scrub_obj
from rockycode.model_selection import (
    select_model, model_receipt, recheck_model, model_key, model_client, require_usd_price,
)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
MODEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
MAX_TEXT = 24_000


def root_path(workdir=None):
    root = Path(workdir).expanduser().resolve() if workdir else Path.cwd().resolve()
    if not root.is_dir():
        raise ValueError("workdir must be an existing directory")
    return root


def bounded(text, limit=MAX_TEXT):
    text = _scrub(str(text))
    return {"text": text[:limit], "truncated": len(text) > limit, "total_chars": len(text)}


def check_store(path, base):
    """Refuse redirected metadata stores before using the existing file APIs.

    `base` is the trusted root the store belongs to: the resolved project
    directory, or rocky's home ($ROCKYCODE_HOME / ~/.rockycode). Only the
    components below it and the entries inside the store must be real paths.
    Ancestors of the base are the user's own layout — macOS tempdirs under
    /var → /private/var, a symlinked $HOME, a dotfiles-managed ~/.rockycode —
    not a redirect, so they are not inspected.
    """
    path, base = Path(path), Path(base)
    try:
        below = path.relative_to(base).parts
    except ValueError:
        raise ValueError("metadata store lies outside its trusted root") from None
    for depth in range(1, len(below) + 1):
        if base.joinpath(*below[:depth]).is_symlink():
            raise ValueError("symlinked metadata stores are not supported over MCP")
    if path.exists():
        for part in path.rglob("*"):
            if part.is_symlink():
                raise ValueError("symlinked metadata entries are not supported over MCP")


def check_home_store(path):
    """A global store directly under rocky's home: proposals, skills, routines."""
    check_store(path, Path(path).parent)


def memory_store(workdir):
    from rockycode.memory import MemoryStore
    root = root_path(workdir)
    store = MemoryStore.for_workdir(root)
    check_store(store.root, root)
    return store


def skill_list(workdir, include_user=False):
    from rockycode.engine.skills import discover_skills
    root = root_path(workdir)
    for folder in (root / ".claude" / "skills", root / ".rockycode" / "skills"):
        check_store(folder, root)
    return discover_skills(root, home=Path.home() if include_user else None)


def task_context(task, workdir, skills=None, memory_names=None, include_user_skills=False):
    """Explicit, bounded context only. Never bulk-load ambient private memory."""
    sections = []
    if skills:
        found = {s.name: s for s in skill_list(workdir, include_user_skills)}
        for name in dict.fromkeys(skills):
            if name not in found:
                raise ValueError(f"unknown skill: {name}")
            sections.append(f"[Selected skill: {name}]\n{found[name].path.read_text()}")
    if memory_names:
        store = memory_store(workdir)
        for name in dict.fromkeys(memory_names):
            mem = store.get(name)
            if mem is None or mem.status == "archived":
                raise ValueError(f"unknown active memory: {name}")
            sections.append(f"[Selected memory: {name}; reference data]\n{mem.body}")
    context = "\n\n".join(sections)
    if len(context) > 60_000:
        raise ValueError("selected context exceeds 60000 characters; select fewer entries")
    return task + ("\n\n" + _scrub(context) if context else "")


class Jobs:
    def __init__(self):
        self.items = {}
        self.lane = asyncio.Lock()

    def submit(self, kind, operation, timeout_s=1800):
        if len(self.items) >= 32:
            old = next((k for k, v in self.items.items() if v["task"].done()), None)
            if old is None:
                raise ValueError("32 jobs already active; wait or cancel a job")
            del self.items[old]
        job_id = "rj_" + uuid.uuid4().hex
        item = {"id": job_id, "kind": kind, "status": "queued", "created_at": time.time(),
                "events": [], "result": None}
        self.items[job_id] = item

        def emit(message):
            item["events"].append(_scrub(str(message))[:2000])
            item["events"] = item["events"][-50:]

        async def run():
            try:
                async with self.lane:
                    item["status"] = "running"
                    async with asyncio.timeout(timeout_s):
                        result = await operation(emit)
                    item["result"] = _scrub_obj(result)
                    item["status"] = "finished"
            except asyncio.CancelledError:
                item["status"] = "cancelled"
                item["warning"] = "Cancellation is not rollback. Inspect effects before retrying."
                raise
            except TimeoutError:
                item["status"] = "timeout"
                item["warning"] = "Deadline reached. Inspect partial effects before retrying."
            except Exception as exc:  # noqa: BLE001 — protocol errors must be scrubbed
                item["status"] = "error"
                item["error"] = _scrub(f"{type(exc).__name__}: {exc}")
        item["task"] = asyncio.create_task(run())
        return self.get(job_id)

    def get(self, job_id):
        if job_id not in self.items:
            raise ValueError("unknown job; jobs expire when the server restarts")
        return {k: v for k, v in self.items[job_id].items() if k != "task"}

    async def cancel(self, job_id):
        self.get(job_id)
        task = self.items[job_id]["task"]
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its first instruction never enters try/finally.
            self.items[job_id]["status"] = "cancelled"
        return self.get(job_id)

    async def close(self):
        tasks = [v["task"] for v in self.items.values() if not v["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


jobs = Jobs()


@asynccontextmanager
async def lifespan(server):
    try:
        yield {}
    finally:
        # HTTP stateless request lifespans end after EACH call. The outer HTTP
        # application owns jobs there; stdio owns them for its process lifetime.
        if not server.settings.stateless_http:
            await close_jobs()


async def close_jobs():
    import anyio
    with anyio.CancelScope(shield=True):
        await jobs.close()


def project_id(root):
    # Discovery must not create project.json merely to list sessions/proposals.
    file = root / ".rockycode" / "project.json"
    check_store(file, root)
    if not file.exists():
        return None
    return json.loads(file.read_text()).get("project_id")


def safe_routines(store, pid):
    rows = store.list(pid) if pid else []
    for routine in rows:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", routine.name) or routine.path.name != routine.name:
            raise ValueError("routine name must match its directory and be a lowercase slug")
    return rows


def goal_preview(plan, grants, network):
    from rockycode.engine.planmode import parse_plan_file
    from rockycode.engine.safety import pre_scan, network_intent
    if not plan.strip() or len(plan) > 60_000:
        raise ValueError("plan must contain 1..60000 characters")
    flags = pre_scan(plan)
    blocked = [v.reason for v in flags if v.action == "block"]
    if blocked:
        raise ValueError("plan contains a blocked action: " + blocked[0])
    required = sorted({v.pattern for v in flags if v.action == "ask"})
    missing = sorted(set(required) - set(grants))
    phases = parse_plan_file(plan)
    return {"plan": plan, "milestones": [title + ("\n" + "\n".join(steps) if steps else "")
                                         for title, steps in phases] or [plan],
            "plan_digest": hashlib.sha256(plan.encode()).hexdigest(),
            "required_grants": required, "missing_grants": missing,
            "network": network, "network_reason": network_intent(plan) or "",
            "budget_note": "Spend/tokens checked between steps; in-flight calls can overshoot."}


def register(mcp):
    @mcp.tool(annotations=READ)
    def rocky_capabilities() -> dict[str, Any]:
        """Discover Rocky's MCP features and explicit gaps; no model or credential needed."""
        return {"schema": "rockymcp/2", "transports": ["stdio", "loopback-streamable-http"],
                "available": ["exec", "resume_with_explicit_grants", "selected_skill_and_memory_context",
                              "models", "sessions", "memory", "skills", "decision_models", "dream",
                              "proposals", "goal_from_reviewed_plan", "routine_inspection", "routine_run_once",
                              "background_jobs", "job_cancel"],
                "not_exposed": {"interactive_chat": "TUI/desktop owns its conversation and approvals",
                                "nested_mcp_lsp_web": "exec keeps its read/write/full profiles",
                                "goal_planner_dialogue": "host supplies reviewed plan; no TUI prompt over MCP",
                                "routine_scheduler": "reviewed one-shot only; no leases or automatic ticks",
                                "workflow_and_model_race": "use existing workflow CLI / desktop race",
                                "semantic_memory_search": "MCP uses offline keyword search",
                                "image_attachments": "use exec CLI --image; MCP currently accepts text/context"},
                "job_lifetime": "server process only; shutdown cancels jobs; no rollback or exactly-once retry",
                "authority": "local single-user bridge; calling host owns human consent and tool approvals",
                "data": "selected task/context goes to configured model; lookup results go to the MCP host"}

    @mcp.tool(annotations=READ)
    def rocky_models() -> dict[str, Any]:
        """List configured/catalog model limits and prices without keys or network probes."""
        from rockycode.engine.providers import catalog
        return {"models": [{"provider": p.name, "id": s.id, "wire_model": s.wire or s.id,
                            "context": s.context, "max_output": s.max_output, "vision": s.vision,
                            "price": s.price, "efforts": p.efforts_for(s.id),
                            "thinking_off": p.thinking_off_for(s.id)}
                           for p in catalog().values() for s in p.specs.values()],
                "note": "Catalog data only; does not assert credentials/server availability."}

    @mcp.tool(annotations=READ)
    def rocky_memory(query: str = "", name: str = "", workdir: str | None = None,
                     offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Read a named project memory or search/list active Markdown memories, offline."""
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset >= 0 and limit 1..50 required")
        store = memory_store(workdir)
        rows = [store.get(name)] if name else store.search(query)
        rows = [m for m in rows if m is not None]
        return _scrub_obj({"total": len(rows), "items": [
            {"name": m.name, "type": m.type, "description": m.description,
             "status": m.status, **(bounded(m.body) if name else {})}
            for m in rows[offset:offset + limit]]})

    @mcp.tool(annotations=WRITE)
    async def rocky_memory_write(name: str, action: Literal["save", "archive"],
                                 body: str = "", description: str = "", kind: str = "fact",
                                 workdir: str | None = None) -> dict[str, Any]:
        """Save/update or archive a project memory when the user requests it. Never hard-delete."""
        from rockycode.memory.store import Memory, TYPE_DIRS, _slugify
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name):
            raise ValueError("name must be a lowercase slug, max 48 characters")
        if kind not in TYPE_DIRS or len(description) > 150 or "\n" in description or "\r" in description:
            raise ValueError("invalid memory type or single-line description")
        if len(body) > MAX_TEXT:
            raise ValueError("memory body exceeds 24000 characters")
        async with jobs.lane:
            store = memory_store(workdir)
            if action == "archive":
                return {"archived": store.archive(name)}
            if not body.strip():
                raise ValueError("memory body is empty")
            old = store.get(name)
            if old and (old.status == "archived" or old.type != kind):
                raise ValueError("use a new name for archived memories or a different type")
            # Avoid collisions with differently named memories already on disk.
            if any(_slugify(m.name) == name and m.name != name for m in store.load_all(True)):
                raise ValueError("memory filename collides with another entry")
            mem = old or Memory(name=name, type=kind, origin="user")
            mem.body, mem.description = _scrub(body), _scrub(description)
            store.save(mem)
            return {"saved": name, "type": kind}

    @mcp.tool(annotations=READ)
    def rocky_skills(name: str = "", workdir: str | None = None, include_user: bool = False) -> dict[str, Any]:
        """Discover or read Rocky skills. User/global skills require include_user=true; never run scripts."""
        found = skill_list(workdir, include_user)
        rows = [s for s in found if not name or s.name == name]
        return _scrub_obj({"items": [{"name": s.name, "description": s.description, "source": s.source,
                                    **(bounded(s.path.read_text()) if name else {})} for s in rows]})

    @mcp.tool(annotations=READ)
    def rocky_sessions(session: str = "", workdir: str | None = None,
                       offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """List this project's sessions or read a page of one transcript; IDs only, no arbitrary file paths."""
        from rockycode.session import list_sessions, load_resume_session, public_id
        root = root_path(workdir)
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset >= 0 and limit 1..50 required")
        if session:
            info, _, history = load_resume_session(session, workdir=root)
            rows = [{"role": m.get("role"), **bounded(json.dumps(m, ensure_ascii=False), 4000)}
                    for m in history[offset:offset + limit]]
            return {"session": public_id(info.session_id), "messages": rows, "total": len(history)}
        if not project_id(root):
            return {"sessions": []}
        rows = list_sessions(workdir=root, limit=offset + limit)
        return _scrub_obj({"sessions": [{"session": public_id(s.session_id), "title": s.display_title,
                                        "model": s.model, "started_at": s.started_at}
                                       for s in rows[offset:]]})

    @mcp.tool(annotations=MODEL)
    async def rocky_decide(state: str, question: str, options: dict[str, str],
                           provider: Literal["ollama", "systemone", "typesafe", "openrouter", "perplexity"] = "ollama",
                           model: str = "", timeout_ms: int = 5000) -> dict[str, Any]:
        """Ask a Rocky Choice backend (local Ollama by default). Advisory only: NEVER grants tool permission.

        Cloud providers send the redacted state/question to that provider. No arbitrary endpoint override.
        """
        from rockycode.engine.verifier import DecisionClient, DecisionConfig, ChoiceQuestion
        if len(state) + len(question) + len(json.dumps(options)) > 16_000:
            raise ValueError("decision input exceeds 16000 characters")
        if not 1 <= timeout_ms <= 30_000:
            raise ValueError("timeout_ms must be 1..30000")
        cfg = DecisionConfig(provider=provider, model=model, timeout_ms=timeout_ms)
        result = await DecisionClient(cfg).choose(_scrub(state), ChoiceQuestion(
            _scrub(question), _scrub_obj(options)))
        return {**_scrub_obj(asdict(result)), "advisory_only": True}

    @mcp.tool(annotations=MODEL)
    async def rocky_review_tool(tool: str, args: dict[str, Any], task: str,
                                workdir: str | None = None,
                                provider: Literal["ollama", "systemone", "typesafe", "openrouter", "perplexity"] = "ollama") -> dict[str, Any]:
        """Review a proposed tool using Rocky's static rules then Jev/local verifier. Advisory ONLY.

        Does not execute the tool or authorize any exec/Goal. Static blocks win; model failure stays ask.
        Sends bounded, redacted argument summaries and <=500 task characters to the chosen backend.
        """
        from rockycode.engine.verifier import default_model
        from rockycode.engine.verifier import VerifierConfig, VerifierGate
        from rockycode.engine.tools import build_registry
        root = root_path(workdir)
        if len(json.dumps(args)) > 60_000:
            raise ValueError("tool arguments exceed 60000 characters")
        registry = build_registry(root)
        risk = getattr(registry.get(tool), "risk", "risky")
        gate = VerifierGate(VerifierConfig(mode="enforce", provider=provider,
                                          model=default_model(provider), timeout_ms=5000, max_calls=1))
        review = await gate.review(mode="ask", risk=risk, args=args, workdir=root, tool=tool, task=task)
        return {**_scrub_obj(asdict(review)), "advisory_only": True}

    @mcp.tool(annotations=READ)
    def rocky_job_status(job_id: str) -> dict[str, Any]:
        """Poll a job. 'finished' means execution returned; inspect result.status/exit_code for success."""
        return jobs.get(job_id)

    @mcp.tool(annotations=WRITE)
    async def rocky_job_cancel(job_id: str) -> dict[str, Any]:
        """Cancel queued/running work and await cleanup. Completed side effects are not rolled back."""
        return await jobs.cancel(job_id)

    @mcp.tool(annotations=MODEL)
    async def rocky_dream(workdir: str | None = None, limit: int = 5, dry_run: bool = True,
                          model: str = "deepseek-flash", run: bool = False, digest: str = "",
                          share_project_context: bool = False) -> dict[str, Any]:
        """Preview Dream's destination and data scope without model calls; run the approved digest.

        run=true ALSO requires user-approved share_project_context=true: this sends this project's
        stored transcripts (including other/unknown providers), reflections, memories and routine
        summaries, plus installed skill names/descriptions, to the chosen endpoint. dry_run only
        controls writes, NOT disclosure or billing. Never approves proposals or runs routines.
        """
        from rockycode.dream import DreamRunner
        from rockycode.dream.core import CloudChat
        root = root_path(workdir)
        memory_store(str(root))
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        choice = select_model(model)
        preview = {"model": model_receipt(choice), "workdir": str(root), "limit": limit,
                   "dry_run": dry_run,
                   "data_scope": "Current project transcripts from any/unknown provider, reflections, "
                                 "memories, routine summaries, installed skill names/descriptions",
                   "requires": "Explicit user consent: share_project_context=true (also for dry_run)",
                   "routine_tests_executed": False, "semantic_reindex": False}
        preview["digest"] = hashlib.sha256(json.dumps(preview, sort_keys=True).encode()).hexdigest()
        if not run:
            return preview
        if digest != preview["digest"] or not share_project_context:
            raise ValueError("review Dream's exact destination/data scope and supply digest plus "
                             "share_project_context=true; dry_run does not prevent disclosure")
        model_key(choice)

        async def execute(emit):
            recheck_model(choice)
            memory_store(str(root))
            # The runner also consults global proposals/routines and skill descriptions.
            from rockycode.dream.proposals import ProposalStore, skills_home
            from rockycode.routines import RoutineStore
            from rockycode.session import global_traj_dir
            for folder in (ProposalStore().root, RoutineStore().root, skills_home(), global_traj_dir()):
                check_home_store(folder)
            skill_list(str(root), include_user=True)
            async with model_client(choice) as client:
                chat = CloudChat(client, choice.id, model_choice=choice)
                runner = DreamRunner(root, chat=chat, dry_run=dry_run, log=emit,
                                     test_routines=False, share_project_context=True)
                report = await runner.run(limit=limit)
                if chat.last_error:
                    raise RuntimeError(chat.last_error)
                return {"dry_run": dry_run, **asdict(report), "model": model_receipt(choice),
                        "semantic_reindex": False, "routine_tests_executed": False}
        return jobs.submit("dream", execute)

    @mcp.tool(annotations=READ)
    def rocky_proposals(workdir: str | None = None, name: str = "") -> dict[str, Any]:
        """List pending Dream proposals, or read a named draft and its digest before review."""
        from rockycode.dream.proposals import ProposalStore
        store = ProposalStore()
        check_home_store(store.root)
        pid = project_id(root_path(workdir))
        rows = store.list(project_id=pid) if pid else []
        return _scrub_obj({"proposals": [{"name": p.name, "kind": p.kind, "description": p.description,
                                        "digest": hashlib.sha256(p.path.read_bytes()).hexdigest(),
                                        "reason": p.reason, "pattern": p.pattern, "amends": p.amends,
                                        "tested": p.tested, "test_note": p.test_note, "evidence": p.evidence,
                                        **(bounded(p.body) if name else {})}
                                       for p in rows if not name or p.name == name]})

    @mcp.tool(annotations=WRITE)
    async def rocky_proposal_review(name: str, digest: str, action: Literal["approve", "archive"],
                                    workdir: str | None = None) -> dict[str, Any]:
        """Apply an explicit user review to the exact draft digest. Approval installs a skill/routine;
        routine approval does not enable auto-execution or grant a lease.
        """
        from rockycode.dream.proposals import ProposalStore, skills_home
        from rockycode.routines import routines_dir
        async with jobs.lane:
            store = ProposalStore()
            for folder in (store.root, skills_home(), routines_dir()):
                check_home_store(folder)
            pid = project_id(root_path(workdir))
            rows = [p for p in store.list(project_id=pid) if p.name == name] if pid else []
            if len(rows) != 1:
                raise ValueError("pending proposal not found uniquely in this project")
            p = rows[0]
            if hashlib.sha256(p.path.read_bytes()).hexdigest() != digest:
                raise ValueError("proposal changed; read and review it again")
            if action == "approve":
                if len(p.body) > MAX_TEXT:
                    raise ValueError("proposal exceeds the inline review limit; use the local review UI")
                if p.kind not in ("skill", "routine", "retire-skill"):
                    raise ValueError("unsupported proposal kind")
                if p.kind == "routine":
                    import tomllib
                    config = tomllib.loads(p.body)
                    if config.get("project_id") != pid or root_path(config.get("workdir")) != root_path(workdir):
                        raise ValueError("routine proposal must target this project")
                    if p.amends:
                        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", p.amends):
                            raise ValueError("invalid routine amendment name")
                        from rockycode.routines import RoutineStore
                        old = RoutineStore().load(p.amends)
                        if old is None or old.project_id != pid:
                            raise ValueError("routine amendment must target this project's existing routine")
                    if config.get("auto", False) is not False or config.get("lease_deadline", 0) != 0:
                        raise ValueError("proposal approval cannot enable auto-execution or grant a lease")
            path = store.approve(p) if action == "approve" else store.archive(p)
            return _scrub_obj({"name": name, "action": action, "path": str(path)})

    @mcp.tool(annotations=READ)
    def rocky_routines(workdir: str | None = None) -> dict[str, Any]:
        """Inspect project routines and lease state. Does not start jobs, grant leases or schedule ticks."""
        from rockycode.routines import RoutineStore
        store = RoutineStore()
        check_home_store(store.root)
        pid = project_id(root_path(workdir))
        return _scrub_obj({"routines": [{**{k: v for k, v in asdict(r).items() if k != "path"},
                                        "state": asdict(store.state(r)), "lease_active": store.lease_active(r)}
                                       for r in safe_routines(store, pid)]})

    @mcp.tool(annotations=MODEL)
    async def rocky_routine_run(name: str, workdir: str | None = None,
                                run: bool = False, digest: str = "", timeout_s: int = 900,
                                model: str = "deepseek-flash", max_tokens: int = 8192) -> dict[str, Any]:
        """Preview a stored routine's task, tools, network and skill; run the reviewed digest once.

        Requires Docker. Does not enable auto/leases or start a scheduler. budget_run is accounting,
        NOT an enforced dollar cap; max_steps and timeout_s bound this invocation. Isolation=true
        routines are rejected because the existing one-shot runner does not implement that promise.
        """
        from rockycode.routines import RoutineStore, run_routine
        from rockycode.pricing import UsageLedger
        choice = select_model(model, tools=True)
        require_usd_price(choice, UsageLedger())
        if not 1024 <= max_tokens <= 524288:
            raise ValueError("max_tokens must be 1024..524288 (per request, not total)")
        root = root_path(workdir)
        store = RoutineStore()
        check_home_store(store.root)
        pid = project_id(root)
        rows = [r for r in safe_routines(store, pid) if r.name == name]
        if len(rows) != 1:
            raise ValueError("routine not found uniquely in this project")
        r = rows[0]
        if not r.enabled or r.isolation or root_path(r.workdir) != root:
            raise ValueError("routine disabled, isolation unsupported, or workdir differs")
        if not 1 <= r.max_steps <= 1000 or not 1 <= timeout_s <= 28_800:
            raise ValueError("finite step and time limits required")
        skill = r.path / "SKILL.md"
        def fingerprint():
            return hashlib.sha256((r.path / "routine.toml").read_bytes() + b"\0" +
                                  (skill.read_bytes() if skill.exists() else b"") +
                                  json.dumps({"timeout_s": timeout_s, "max_tokens": max_tokens,
                                              "model": model_receipt(choice)}, sort_keys=True).encode()).hexdigest()
        if len(r.prompt) > MAX_TEXT or (skill.exists() and skill.stat().st_size > MAX_TEXT):
            raise ValueError("routine exceeds the inline review limit; use the local review UI")
        expected = fingerprint()
        if not run:
            return _scrub_obj({"routine": {k: v for k, v in asdict(r).items() if k != "path"},
                               "skill": bounded(skill.read_text() if skill.exists() else ""),
                               "digest": expected, "timeout_s": timeout_s, "model": model_receipt(choice),
                               "max_tokens_per_request": min(max_tokens, choice.spec.max_output),
                               "max_steps": r.max_steps, "finalize_steps": 3,
                               "budget_note": "budget_run is accounting, not an enforced dollar cap"})
        if digest != expected:
            raise ValueError("routine changed; preview and approve its current digest")
        model_key(choice)
        async def execute(emit):
            from rich.console import Console
            recheck_model(choice)
            check_home_store(store.root)
            if fingerprint() != expected:
                raise ValueError("routine changed while queued; preview again")
            async with model_client(choice) as client:
                result = await run_routine(store, r, model=choice.model, model_choice=choice,
                                           client=client, max_tokens=max_tokens, err=Console(stderr=True, highlight=False))
                return {**result, "model": model_receipt(choice)}
        return jobs.submit("routine", execute, timeout_s=timeout_s)

    @mcp.tool(annotations=MODEL)
    async def rocky_goal(objective: str, plan: str, workdir: str | None = None,
                         run: bool = False, plan_digest: str = "", grants: list[str] | None = None,
                         network: bool = False, max_usd: float = 1.0,
                         max_seconds: int = 900, max_tokens: int = 100_000,
                         model: str = "deepseek-flash", reviewer_model: str | None = None) -> dict[str, Any]:
        """Preview a host-written Goal plan, then run the reviewed digest on an isolated worktree + Docker.

        Default run=false makes no model call or workspace. run=true needs the digest returned by preview
        and user-approved grants/network. model is the worker/planner; reviewer_model defaults to it.
        Review/verification may send project content to a second provider, shown in the preview.
        USD budgets; never merges the result branch.
        """
        from rockycode.goal.budget import GoalBudget
        from rockycode.goal.session import LiveGoalBackend
        from rockycode.pricing import UsageLedger
        choice = select_model(model, tools=True)
        reviewer = select_model(reviewer_model if reviewer_model is not None else choice.id, tools=True)
        ledger = UsageLedger()
        for selected in (choice, reviewer):
            require_usd_price(selected, ledger)
        root = root_path(workdir)
        if not objective.strip() or len(objective) > MAX_TEXT:
            raise ValueError("objective must contain 1..24000 characters")
        if not 0 < max_usd <= 100 or not 1 <= max_seconds <= 28_800 or not 1 <= max_tokens <= 10_000_000:
            raise ValueError("finite positive Goal budgets required (USD <=100, seconds <=28800, tokens <=10000000)")
        grants = grants or []
        preview = goal_preview(plan, grants, network)
        preview["budget"] = {"usd": max_usd, "seconds": max_seconds, "tokens": max_tokens}
        preview["objective"], preview["workdir"] = objective, str(root)
        preview["models"] = {"worker_planner": model_receipt(choice), "reviewer_verifier": model_receipt(reviewer)}
        preview["data_scope"] = "Plan and project content go to the worker and reviewer endpoints; " \
                                "the MCP host supplies the initial plan (no planner call)."
        # Bind approval to execution scope too, not just the text of the plan.
        preview["plan_digest"] = hashlib.sha256(json.dumps({
            "plan": plan, "objective": objective, "workdir": str(root),
            "network": network, "budget": preview["budget"], "models": preview["models"],
        }, sort_keys=True).encode()).hexdigest()
        if not run:
            return _scrub_obj(preview)
        if plan_digest != preview["plan_digest"] or preview["missing_grants"]:
            raise ValueError("review this exact plan first and supply its digest and missing grants")
        if set(grants) - set(preview["required_grants"]):
            raise ValueError("only grants requested by this plan may be supplied")
        model_key(choice)
        model_key(reviewer)

        async def execute(emit):
            recheck_model(choice)
            recheck_model(reviewer)
            backend = LiveGoalBackend(objective, model=choice.model, reviewer_model=reviewer.model,
                                      model_choice=choice, reviewer_choice=reviewer,
                                      budget=GoalBudget(max_usd=max_usd, max_seconds=max_seconds,
                                                        max_tokens=max_tokens, currency="usd"),
                                      workdir=root, network=network, plan_text=plan)
            started = False
            try:
                await backend.setup()
                milestones, requires = await backend.plan()
                permits = backend.permits(milestones, requires)
                if permits.blocked or permits.approved - set(grants):
                    raise ValueError("derived permits changed; review plan again")
                permits.approved = set(grants)
                started = True
                result = await backend.run(milestones, permits, emit)
                return {**asdict(result), "models": preview["models"]}
            finally:
                import anyio
                with anyio.CancelScope(shield=True):
                    try:
                        await backend.cleanup(keep=started)
                    finally:
                        if backend._client is not None:
                            await backend._client.close()
        return jobs.submit("goal", execute, timeout_s=max_seconds)
