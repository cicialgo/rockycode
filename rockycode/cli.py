"""rockycode CLI: `rockycode bench …`"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console

from rockycode.banner import confused, fail, info, show_banner

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.onboarding import (  # noqa: E402
    bootstrap_credentials,
    project_env_warnings,
    require_base_url,
    require_key,
)
bootstrap_credentials()

# `rockycode` without args → `rockycode chat`; a leading flag (`rockycode
# --resume …`, `rockycode --yolo`) reaches chat too — nobody should have to
# remember to type "chat" first.
_TOP_LEVEL_FLAGS = {"--help", "-h", "--version", "--install-completion", "--show-completion"}
if len(sys.argv) == 1:
    sys.argv.append("chat")
elif sys.argv[1].startswith("-") and sys.argv[1] not in _TOP_LEVEL_FLAGS:
    sys.argv.insert(1, "chat")
# Bare `--resume` (no id following) means "open the picker". Typer options
# can't be both valueless and take a value, so rewrite the bare form to a
# sentinel the chat command understands.
for _i, _a in enumerate(sys.argv):
    if _a in ("--resume", "-r") and (_i == len(sys.argv) - 1 or sys.argv[_i + 1].startswith("-")):
        sys.argv[_i] = "--resume=__pick__"

app = typer.Typer(
    help="rockycode — a coding agent harness benchmarked on SWE-bench Verified.",
    no_args_is_help=False,
)
console = Console()

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode."""


def _load_task_ids(tasks: str) -> Optional[list[str]]:
    """Resolve --tasks into an instance_id list, or None for the full Verified set."""
    if tasks == "verified":
        return None
    if tasks == "dev10":
        path = REPO_ROOT / "bench" / "tasks" / "dev10.json"
    else:
        path = Path(tasks)
    if not path.exists():
        raise typer.BadParameter(f"task file not found: {path}")
    return json.loads(path.read_text())


def _load_prompt(prompt_path: Optional[Path]) -> tuple[str, str, str]:
    """Resolve --prompt into (system_prompt, name, sha8).

    Default is the built-in ROCKY_SYSTEM; a file swaps it wholesale. The
    name+sha land in trajectory meta so A/B runs stay distinguishable.
    """
    import hashlib

    if prompt_path is None:
        from rockycode.prompts.rocky import ROCKY_SYSTEM
        text, name = ROCKY_SYSTEM, "rocky-builtin"
    else:
        if not prompt_path.exists():
            raise typer.BadParameter(f"prompt file not found: {prompt_path}")
        text, name = prompt_path.read_text(), prompt_path.stem
    sha = hashlib.sha256(text.encode()).hexdigest()[:8]
    return text, name, sha


def _docker_preflight() -> None:
    """Bail with a friendly message if the Docker daemon isn't reachable.

    Runs before any API call so the user doesn't burn tokens on a run
    that was always going to fail at the scoring step.
    """
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        fail(console, "`docker` command not found.")
        info(console, "install Docker Desktop (https://www.docker.com/products/docker-desktop)")
        info(console, "or pass --skip-score to generate predictions without scoring.")
        raise typer.Exit(1)
    except subprocess.TimeoutExpired:
        fail(console, "docker daemon check timed out (10s).")
        info(console, "is Docker Desktop running? open it and wait for the whale icon to be steady.")
        raise typer.Exit(1)

    if proc.returncode != 0:
        fail(console, "docker daemon not reachable. scoring needs docker.")
        info(console, "open Docker Desktop and wait for the whale 🐳 in your menu bar to be steady.")
        info(console, "or pass --skip-score to generate predictions only.")
        first_err = (proc.stderr or proc.stdout or "").strip().splitlines()
        if first_err:
            info(console, f"detail: {first_err[0]}")
        raise typer.Exit(1)


@app.command()
def chat(
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-C", help="Project directory rocky works in. Defaults to cwd."
    ),
    allow_dir: Optional[List[Path]] = typer.Option(
        None, "--allow-dir",
        help="Extra directory read/write/edit may touch, beyond the workdir "
             "(repeatable). For multi-root setups (a sibling package, a shared "
             "config dir). Declared here at launch only — a project's config can "
             "never widen its own jail. Outside these roots, use the (gated) bash tool.",
    ),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt", help="System prompt file (default: built-in ROCKY_SYSTEM)."
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", "-r",
        help="Resume a session: bare --resume opens the picker (newest first; "
             "^a for all folders); or give an id from the exit card (rk_…).",
    ),
    mcp: bool = typer.Option(
        True, "--mcp/--no-mcp",
        help="Load MCP servers from .mcp.json / Claude Code / Claude Desktop / Codex configs.",
    ),
    skills: bool = typer.Option(
        True, "--skills/--no-skills",
        help="Load skills from .claude/skills, .rockycode/skills, ~/.claude/skills, ~/.codex/prompts.",
    ),
    memory: bool = typer.Option(
        True, "--memory/--no-memory",
        help="Load project memory from .rockycode/memory (see `rockycode memory --help`).",
    ),
    web: bool = typer.Option(
        True, "--web/--no-web",
        help="Enable web_search/web_research/web_fetch tools (search runs on DeepSeek's "
             "Anthropic endpoint; env: ROCKYCODE_SEARCH_MODEL, ROCKYCODE_SEARCH_ORDER).",
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
        help="Enable DeepSeek thinking mode (env: ROCKYCODE_THINKING).",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
        help="Reasoning depth when thinking is on: high | xhigh | max. The dial is "
             "provider-neutral; DeepSeek only knows high/max, so xhigh sends max "
             "(env: ROCKYCODE_REASONING_EFFORT).",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 384_000),
        "--max-tokens",
        help="Max output tokens per call, incl. thinking/CoT (default = DeepSeek V4's "
             "384K max, so it never truncates; env: ROCKYCODE_MAX_TOKENS).",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 1048576),
        "--context-window",
        help="Model context window in tokens (DeepSeek V4 = 1M). Soft reminder at "
             "50% (V4 degrades past the half); auto-compacts near full (env: "
             "ROCKYCODE_CONTEXT_WINDOW).",
    ),
    sandbox: bool = typer.Option(
        False, "--sandbox/--no-sandbox",
        help="Start rocky inside a Docker sandbox container (isolates tool execution).",
    ),
    sandbox_network: bool = typer.Option(
        False, "--sandbox-network/--no-sandbox-network",
        help="Give the sandbox network access (default OFF — offline, no egress). "
             "Only with --sandbox.",
    ),
    lsp: bool = typer.Option(
        True, "--lsp/--no-lsp",
        help="Connect to an LSP MCP server for diagnostics in read_file (env: ROCKYCODE_LSP_COMMAND).",
    ),
    live: bool = typer.Option(
        os.getenv("ROCKYCODE_ARTIFACTS_LIVE", "").lower() in ("1", "true", "yes"),
        "--live/--no-live",
        help="Serve artifacts live: open them in a local browser tab that "
             "auto-refreshes when rocky rebuilds them. Default off — rocky asks "
             "once on the first artifact. env: ROCKYCODE_ARTIFACTS_LIVE.",
    ),
    permission: Optional[str] = typer.Option(
        None, "--permission",
        help="Tool-approval strictness: yolo | ask | careful. Overrides config (default: ask).",
    ),
    yolo: bool = typer.Option(
        False, "--yolo",
        help="Shortcut for --permission yolo: never prompt before running tools. "
             "Unsafe with untrusted skills/repos — only use when you trust the tool calls.",
    ),
    max_steps: int = typer.Option(
        _env_int("ROCKYCODE_CHAT_MAX_STEPS", 0),
        "--max-steps",
        help="Tool-step cap per turn; 0 = unlimited (default — runs until done, bounded "
             "by context compaction; cancel anytime by sending a new message). Set a "
             "number for a hard ceiling. env: ROCKYCODE_CHAT_MAX_STEPS.",
    ),
) -> None:
    """Talk to rocky: the interactive agent TUI. amaze!"""
    from rockycode.onboarding import run_setup
    run_setup(console)  # first run: paste-your-key, then continue
    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        fail(console, "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(1)
    if reasoning_effort not in {"high", "xhigh", "max"}:
        fail(console, f"invalid --reasoning-effort '{reasoning_effort}'. use high, xhigh, or max.")
        raise typer.Exit(1)

    # Textual requests the kitty keyboard protocol (report-all-keys); iTerm2
    # honors it and then bypasses IME composition, so Chinese/Japanese input
    # never reaches the app (textual#6552). Legacy key reporting loses us
    # nothing — basic bindings only. Must be set before textual is imported;
    # export TEXTUAL_DISABLE_KITTY_KEY=0 to opt back in.
    os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

    from rockycode.engine import Engine
    from rockycode.tui.app import run_app

    wd = (workdir or Path.cwd()).resolve()

    # --resume <id>: resolve BEFORE the engine exists — the session names its
    # project, and we land in that project's CURRENT folder (the registry
    # survives renames), no matter where the command ran from.
    resume_pick = resume == "__pick__"
    resume_info = None
    if resume and not resume_pick:
        from rockycode.session import project_current_path, public_id, resolve_session
        resume_info, err = resolve_session(resume)
        if resume_info is None:
            fail(console, err)
            raise typer.Exit(1)
        target = project_current_path(resume_info.project_id) or (
            Path(resume_info.project_path) if resume_info.project_path else None)
        if target is None or not target.is_dir():
            fail(console, f"{public_id(resume_info.session_id)}'s folder is gone "
                          f"(last known: {resume_info.project_path or 'unknown'}).")
            raise typer.Exit(1)
        if target != wd:
            info(console, f"session {public_id(resume_info.session_id)} lives in {target} — starting there.")
            wd = target

    # Extra file-tool roots the human declared at launch (--allow-dir). Resolved
    # and validated here so the jail compares against real directories; a bad
    # path fails loudly rather than silently doing nothing.
    allowed_roots: tuple[Path, ...] = ()
    if allow_dir:
        roots = []
        for d in allow_dir:
            rp = d.expanduser().resolve()
            if not rp.is_dir():
                fail(console, f"--allow-dir '{d}' is not a directory.")
                raise typer.Exit(1)
            roots.append(rp)
        allowed_roots = tuple(roots)

    system_prompt, prompt_name, prompt_sha = _load_prompt(prompt)

    # Stable project identity (survives folder rename) + global registry so
    # sessions are discoverable across folders for --resume.
    from rockycode.session import get_project
    project = get_project(wd)

    from rockycode.config import load as load_config
    cfg = load_config(wd)

    # zh sessions get the Chinese BASE prompt (arm-4 shape: zh base for
    # identity + the 语言要求 closer, appended later, for adherence). Only the
    # builtin swaps — an explicit --prompt file always wins as-is. Chat only:
    # bench never reads the language config.
    if prompt is None and cfg["language"] == "zh":
        import hashlib
        from rockycode.prompts.rocky import ROCKY_SYSTEM_ZH
        system_prompt, prompt_name = ROCKY_SYSTEM_ZH, "rocky-builtin-zh"
        prompt_sha = hashlib.sha256(system_prompt.encode()).hexdigest()[:8]

    # CLI flags override config: --yolo wins, else --permission, else config key.
    perm = "yolo" if yolo else (permission or cfg["permission"])
    if perm not in {"yolo", "ask", "careful"}:
        fail(console, f"invalid --permission '{perm}'. use yolo | ask | careful.")
        raise typer.Exit(1)
    # A cloned/untrusted project's .rockycode/config.toml can lower the mode. We
    # honor it but flag it loudly (a persistent chip + a startup warning).
    # load_config() with no workdir = defaults<-global only, so a weaker rank than
    # that — and no explicit CLI flag — means the project file lowered the guard.
    _rank = {"careful": 2, "ask": 1, "yolo": 0}
    perm_weakened = (
        not yolo and permission is None
        and _rank.get(perm, 1) < _rank.get(load_config()["permission"], 1)
    )

    from rockycode.pricing import UsageLedger
    ledger = UsageLedger()

    # Project instructions: same files Claude Code / Codex users already have.
    notes_loaded = []
    for fname in ("CLAUDE.md", "AGENTS.md"):
        p = wd / fname
        if p.exists():
            system_prompt += f"\n\n# Project instructions (from {fname})\n\n{p.read_text()[:20000]}"
            notes_loaded.append(fname)

    skill_list = []
    if skills:
        from rockycode.engine.skills import discover_skills, skills_prompt_section
        skill_list = discover_skills(wd, home=Path.home())
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
        model=model,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        context_window=context_window,
        max_steps=max_steps,
        workdir=wd,
        allowed_roots=allowed_roots,
        system_prompt=system_prompt,
        trajectory_meta={
            "source": "chat",
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
    engine.memory_store = mem_store
    if mem_store is not None:
        from rockycode.memory import build_memory_tools
        from rockycode.memory.index import IndexUnavailable, MemoryIndex
        try:
            mem_index = MemoryIndex(mem_store)
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
        servers, notices = discover(wd)
        engine.mcp_manager = MCPManager(servers, notices)

    # LSP — config resolved here; connection is started async in the TUI
    # (same two-phase pattern as MCP: construct here, start in on_mount).
    lsp_mgr = None
    engine.lsp_enabled = lsp
    engine.lsp_manager = None
    if lsp:
        from rockycode.engine.lsp import MultiTenantLSPManager, resolve_lsp_config
        lsp_cfg = resolve_lsp_config()
        if lsp_cfg:
            cmd, args = lsp_cfg
            engine.lsp_manager = MultiTenantLSPManager(cmd, args)
            info(console, f"lsp: server configured ({cmd}) — connecting in TUI…")
        # LSP is opt-in; when it's not configured (the default) stay quiet rather
        # than nag every startup. `/lsp` shows status on demand.

    # Artifact live-mode state. The create_artifact tool itself is registered
    # after any sandbox swap (below) so it stays a host tool. Static file:// by
    # default; live (localhost + auto-reload) lazy-starts on the first artifact
    # (asked once) or from the start with --live.
    engine.artifact_live = True if live else None  # None = ask on first artifact
    engine.artifact_server = None

    sb = None
    if sandbox:
        import asyncio as _asyncio
        from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
        info(console, "sandbox: starting container…")
        try:
            sb = _asyncio.run(ChatSandbox.start(wd, network=sandbox_network))
            engine.swap_registry(build_sandbox_registry(sb, extras=web_tools))
            net = "network on" if sandbox_network else "offline (no network)"
            info(console, f"sandbox: ready ({sb.container_id[:12]}…) — tools run in /workspace · {net}")
        except Exception as e:
            fail(console, f"sandbox start failed: {e}")
            raise typer.Exit(1)

    # Inject LSP diagnostics into read_file (after sandbox swap, so the
    # active registry — local or sandbox — gets the injection).
    # Safe to apply eagerly: get_diagnostics returns "" until LSP connects.
    if engine.lsp_manager is not None and "read_file" in engine.registry:
        _original_read = engine.registry["read_file"].fn
        _read_schema = engine.registry["read_file"].schema
        _lsp = engine.lsp_manager

        async def _read_with_diag(path: str, offset=None, limit=None) -> str:
            result = await _original_read(path, offset=offset, limit=limit)
            if result.startswith("[error]") or result.startswith("[directory]"):
                return result
            try:
                diag = await _lsp.get_diagnostics(path)
            except Exception:  # noqa: BLE001
                diag = ""
            if diag:
                result = result + diag
            return result

        from rockycode.engine.tools import Tool
        engine.registry["read_file"] = Tool(
            # risk="safe" must survive the rewrap: the Tool default is "risky",
            # which would silently break read-batch parallelism AND make the
            # permission layer start gating plain reads whenever LSP is on.
            name="read_file", schema=_read_schema, fn=_read_with_diag, risk="safe",
        )

    # Artifact tool — host tool (writes to host fs, opens host browser); added
    # after any sandbox swap so it survives there too.
    from rockycode.engine.artifact import build_artifact_tools
    for tool in build_artifact_tools(workdir=wd, engine=engine).values():
        engine.registry[tool.name] = tool

    # Goal review/merge — host tools that act on the real repo (git), so a /goal
    # branch can be reviewed and safely merged from chat. Host-side, so they run
    # against the origin repo even when the chat tools are sandboxed. The reviewer
    # makes review_goal_branch buy a grounded, citation-checked review from a
    # read-only explore child (reads the branch via git refs — host-side, so this
    # holds in sandbox mode too) instead of dumping a 40k-char raw diff into chat.
    from rockycode.engine.explore import make_branch_reviewer
    from rockycode.engine.goal_review import build_goal_tools
    for tool in build_goal_tools(workdir=wd, reviewer=make_branch_reviewer(engine)).values():
        engine.registry[tool.name] = tool

    # explore — buy a read-only, citation-verified investigation instead of
    # grepping it into this context (engine/explore.py). Skipped IN sandbox
    # mode: children read the HOST tree, which would quietly bypass the
    # container the user asked for; sandbox-aware children are a follow-up.
    if sb is None:
        from rockycode.engine.explore import build_explore_tool
        engine.registry.update(build_explore_tool(engine))

    # Finalize the system prompt now that the registry is complete: the tools
    # section is GENERATED from what actually registered (the old hand-written
    # sentence advertised web/artifact tools in bench and under --no-web where
    # they never existed), then language (config auto|en|zh — resolved once,
    # prefix stays byte-stable), one environment line, and the date stamp.
    # Order puts the zh 语言要求 block near the end: recency beats the English
    # sections above it. Must run BEFORE the launch-mode swap below, so the
    # mode contract layers on the final base.
    from rockycode.prompts.rocky import tools_section, with_environment, with_language, with_today
    _final = engine._base_system + tools_section(engine.registry)
    _final = with_environment(_final, wd)
    _final = with_language(_final, cfg["language"])
    _final = with_today(_final)  # chat only — bench stays date-free
    engine.set_base_system(_final)

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
            info(console, f"config mode '{cfg['mode']}' is not a built-in mode — ignored.")

    # /goal now runs INSIDE the app in its own screen (plan → confirm → work →
    # summary), then pops back to chat — no exit, no bare terminal, no subprocess.
    run_app(
        engine, resume=resume_pick, resume_session=resume_info, sandbox=sb,
        currency=cfg["currency"], theme=cfg["theme"],
        permission=perm, permission_weakened=perm_weakened,
        exit_sheet=cfg["exit_sheet"], dream=cfg["dream"],
    )
    engine.finalize_outcome()  # heuristic outcome record (self-evolve phase 0)
    _print_exit_card(engine)


def _print_exit_card(engine) -> None:
    """The resume handoff: after the app closes, print the session's id, title,
    and folder plus the exact way back — full commands, long flags only (short
    flags are unmemorable; see the exit-card design in docs/resume-design.md)."""
    path = getattr(getattr(engine, "trajectory", None), "path", None)
    if path is None:
        return  # logging was disabled (unwritable store) — nothing to resume
    from rich.markup import escape

    from rockycode.palette import PURPLE
    from rockycode.session import _read_info, public_id
    s = _read_info(Path(path))
    if s is None or s.summary == "(no message)":
        return  # no user turn ever happened — nothing worth resuming
    sid = public_id(s.session_id)
    folder = Path(s.project_path).name if s.project_path else s.project_name
    console.print(
        f"[bold {PURPLE}]♪ session saved[/] · [bold]{sid}[/] · "
        f"“{escape(s.display_title)}” · 📁 {folder} · {s.n_messages} msgs"
    )
    console.print(f"  resume it:  [cyan]rockycode --resume {sid}[/]")
    console.print(f"  or browse:  [cyan]rockycode --resume[/]")


# exec's local error exit — mirrors headless.EXIT_ERROR without importing the
# engine at module import time (cli.py must stay fast for --help).
EXIT_CODE_ERROR = 1


@app.command("exec")
def exec_cmd(
    prompt: Optional[str] = typer.Argument(
        None,
        help="The task. Omit or pass '-' to read it from stdin (for long prompts "
             "piped by a calling agent).",
    ),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-C", help="Project directory rocky works in. Defaults to cwd."
    ),
    allow_dir: Optional[List[Path]] = typer.Option(
        None, "--allow-dir",
        help="Extra directory write/edit may touch beyond the workdir (repeatable).",
    ),
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    max_steps: int = typer.Option(
        30, "--max-steps",
        help="Tool-step budget (must be > 0 — headless runs are never unbounded). "
             "Exhaustion exits 3 with the session id; a caller can retry bigger.",
    ),
    output_last_message: Optional[Path] = typer.Option(
        None, "--output-last-message", "-o",
        help="Also write the final answer text to this file.",
    ),
    include_thinking: bool = typer.Option(
        False, "--include-thinking",
        help="Emit DeepSeek reasoning deltas as `thinking` events (off by default — "
             "they bloat the calling agent's context).",
    ),
    originator: str = typer.Option(
        "", "--originator",
        help="Calling agent self-identification (e.g. 'claude-code'), recorded in "
             "the trajectory's audit trail. env: ROCKYCODE_ORIGINATOR.",
    ),
    sandbox: bool = typer.Option(
        True, "--sandbox/--no-sandbox",
        help="Run every tool inside a Docker container (default ON). The task can "
             "come from an untrusted source, so isolation — not the command "
             "classifier — is the real boundary. --no-sandbox runs on the host "
             "(UNSAFE for untrusted input; needs no Docker).",
    ),
    network: bool = typer.Option(
        False, "--network/--no-network",
        help="Give the sandbox network access (default OFF — no egress, so a "
             "delegated task can't exfiltrate or phone home). Turn on only when "
             "the task genuinely needs to fetch something.",
    ),
) -> None:
    """Headless one-shot for OTHER coding agents: run one task, stream JSONL, exit.

    stdout is JSONL only. First line: `meta` {schema: rockyexec/1, session:
    rk_…, profile}. Then `text` / `tool.started` / `tool.finished` / `error`
    events. Last line: `result` {status, summary, blocked_on, evidence:
    {files_changed, commands, refused}, usage} — evidence, not verdicts:
    the caller verifies. Everything human goes to stderr.

    Exit codes: 0 done · 1 error · 2 blocked on an action needing a grant
    (result.blocked_on.grant says which) · 3 step budget spent.

    Permissions are workspace-write: edits stay inside --workdir (+
    --allow-dir roots). Destructive/irreversible commands are always refused —
    no flag disables that. Deletes, pushes, installs, and sudo stop the run
    at exit 2; --resume + --allow to grant-and-continue land in phase 2.
    """
    err = Console(stderr=True)  # stdout belongs to the JSONL contract
    try:
        require_key()
    except Exception as e:  # noqa: BLE001 — one friendly line, no traceback
        fail(err, str(e))
        raise typer.Exit(EXIT_CODE_ERROR)
    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        fail(err, "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(EXIT_CODE_ERROR)
    if max_steps <= 0:
        fail(err, "--max-steps must be > 0: headless runs are never unbounded.")
        raise typer.Exit(EXIT_CODE_ERROR)

    if prompt is None or prompt == "-":
        if sys.stdin.isatty():
            fail(err, "no task. pass it as an argument or pipe it on stdin.")
            raise typer.Exit(EXIT_CODE_ERROR)
        prompt = sys.stdin.read()
    if not prompt.strip():
        fail(err, "empty task.")
        raise typer.Exit(EXIT_CODE_ERROR)

    wd = (workdir or Path.cwd()).resolve()
    if not wd.is_dir():
        fail(err, f"--workdir '{wd}' is not a directory.")
        raise typer.Exit(EXIT_CODE_ERROR)
    allowed_roots: tuple[Path, ...] = ()
    if allow_dir:
        roots = []
        for d in allow_dir:
            rp = d.expanduser().resolve()
            if not rp.is_dir():
                fail(err, f"--allow-dir '{d}' is not a directory.")
                raise typer.Exit(EXIT_CODE_ERROR)
            roots.append(rp)
        allowed_roots = tuple(roots)

    # Project identity: exec sessions land in the same global trajectory store
    # and resume picker as chat sessions — the receipt must be resumable.
    from rockycode.session import get_project
    get_project(wd)

    import asyncio

    from rockycode.engine.headless import run_exec

    code = asyncio.run(run_exec(
        prompt=prompt, model=model, workdir=wd, allowed_roots=allowed_roots,
        max_steps=max_steps, originator=originator,
        include_thinking=include_thinking, output_last_message=output_last_message,
        sandbox=sandbox, network=network, err=err,
    ))
    raise typer.Exit(code)


@app.command()
def config(
    key: Optional[str] = typer.Argument(None, help="Config key to read or set."),
    value: Optional[str] = typer.Argument(None, help="New value (omit to read)."),
) -> None:
    """Show or set rockycode preferences (currency, theme, language)."""
    from rockycode.config import DEFAULTS, GLOBAL_PATH, load, set_value
    if key is None:
        resolved = load(Path.cwd())
        info(console, f"config file: {GLOBAL_PATH}")
        for k in DEFAULTS:
            console.print(f"  [cyan]{k}[/cyan] = {resolved[k]}")
        return
    if value is None:
        console.print(f"{key} = {load(Path.cwd()).get(key)}")
        return
    v, err = set_value(key, value)
    if err:
        fail(console, err)
        raise typer.Exit(1)
    info(console, f"saved: {key} = {v}  →  {GLOBAL_PATH}")


@app.command()
def pricing() -> None:
    """Show the token price table (USD + CNY) and peak-hour status."""
    from datetime import datetime, timezone

    from rockycode.pricing import (
        OVERRIDE_PATH, PRICING_SOURCE_URL, PRICING_VERIFIED, _is_peak, load_pricing,
    )
    p = load_pricing()
    info(console, f"verified {PRICING_VERIFIED} · source {PRICING_SOURCE_URL}")
    info(console, f"edit to update (no reinstall): {OVERRIDE_PATH}")
    console.print()
    for model, rates in p["models"].items():
        console.print(f"  [cyan]{model}[/cyan] [dim](per 1M tokens)[/dim]")
        for cur in ("usd", "cny"):
            r = rates.get(cur)
            if not r:
                console.print(f"    {cur.upper()}: [dim]not set[/dim]")
                continue
            sym = "¥" if cur == "cny" else "$"
            console.print(
                f"    {cur.upper()}: in-hit {sym}{r['in_hit']} · "
                f"in-miss {sym}{r['in_miss']} · out {sym}{r['out']}"
            )
    peak = p.get("peak", {})
    console.print()
    if peak.get("enabled"):
        wins = ", ".join(f"{w['start']}–{w['end']}" for w in peak.get("windows_utc", []))
        active = "ACTIVE now" if _is_peak(datetime.now(timezone.utc), peak) else "not active right now"
        console.print(
            f"  [cyan]peak surcharge[/cyan] ×{peak.get('multiplier')} · UTC {wins} · "
            f"from {peak.get('effective_date', '?')} · [dim]{active}[/dim]"
        )
    else:
        console.print("  [cyan]peak surcharge[/cyan] [dim]disabled[/dim]")


@app.command()
def dream(
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-C", help="Project directory. Defaults to cwd."),
    model: str = typer.Option(
        os.getenv("ROCKYCODE_DREAM_MODEL", "qwen3.5:2b"),
        "--model",
        help="Local Ollama model for consolidation (env: ROCKYCODE_DREAM_MODEL; "
             "2b default — the eager miner; bigger sizes decline more, see core.py).",
    ),
    limit: int = typer.Option(10, "--limit", help="Max sessions to digest in one pass."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show decisions without writing anything."),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the cloud judge pass (fully local dream)."),
) -> None:
    """Rocky sleeps: digest recent sessions into memory on a local model. ♪zzz"""
    import asyncio as _asyncio

    from rockycode.dream import DreamRunner

    wd = (workdir or Path.cwd()).resolve()
    console.print("[dim]♪zzz… rocky sleep. u watch something. i sort memory.[/dim]")

    # The transcript judge (self-evolve): one cheap cloud call per pending
    # session, appended as an outcome record. Strictly optional — no key or
    # no ROCKYCODE_MODEL and the dream stays fully local, exactly as before.
    judge = None
    if not no_judge and not dry_run:
        judge_model = os.getenv("ROCKYCODE_MODEL")
        try:
            if judge_model:
                from openai import AsyncOpenAI

                from rockycode.dream.judge import TranscriptJudge
                from rockycode.onboarding import require_base_url, require_key
                # Explicit key AND endpoint, same rule as Engine: the ambient
                # env must never decide where the key is sent (env-namespace).
                judge = TranscriptJudge(
                    AsyncOpenAI(api_key=require_key(), base_url=require_base_url(),
                                max_retries=3, timeout=120.0),
                    model=judge_model,
                )
        except Exception:  # noqa: BLE001 — keyless dream stays local, no nagging
            judge = None

    runner = DreamRunner(wd, model=model, dry_run=dry_run, judge=judge,
                         log=lambda s: console.print(f"  [dim]♪ {s}[/dim]"))

    index = None
    from rockycode.memory.index import IndexUnavailable, MemoryIndex
    try:
        index = MemoryIndex(runner.store)
        index.conn()
    except IndexUnavailable:
        index = None

    try:
        report = _asyncio.run(runner.run(limit=limit, index=index))
    except RuntimeError as e:
        confused(console, str(e))
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — usually Ollama not running
        fail(console, f"dream failed ({type(e).__name__}: {e}). is ollama running?")
        raise typer.Exit(1)

    for line in report.decisions:
        console.print(f"  [dim]· {line}[/dim]")
    summary = (
        f"{report.sessions_digested} session(s) digested · "
        f"facts +{report.facts_added} ~{report.facts_updated} "
        f"archived {report.facts_archived} noop {report.facts_noop}"
    )
    if report.sessions_judged:
        summary += f" · judged {report.sessions_judged}"
    if report.weaknesses_added or report.weaknesses_reinforced:
        summary += f" · weaknesses +{report.weaknesses_added} ~{report.weaknesses_reinforced}"
    if report.proposals_drafted:
        summary += f" · drafted {report.proposals_drafted} proposal(s)"
        console.print("  [dim]· review drafts inside rockycode with /proposals[/dim]")
    if report.reindexed:
        summary += f" · re-embedded {report.reindexed[0]}"
    if dry_run:
        info(console, f"[dry-run] {summary}")
    elif report.sessions_digested == 0:
        info(console, "nothing new to dream about. rocky already remember everything.")
    else:
        console.print(f"[bold]✦ amaze! i wake. i remember better now.[/bold]  [dim]{summary}[/dim]")


memory_app = typer.Typer(
    help="Inspect rocky's memory. Files under .rockycode/memory are the truth — edit them freely.",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")


def _store(workdir: Optional[Path]) -> "MemoryStore":  # noqa: F821 — lazy import below
    from rockycode.memory import MemoryStore
    return MemoryStore.for_workdir((workdir or Path.cwd()).resolve())


_WORKDIR_OPT = typer.Option(None, "--workdir", "-C", help="Project directory. Defaults to cwd.")


@memory_app.command("list")
def memory_list(workdir: Optional[Path] = _WORKDIR_OPT, all: bool = typer.Option(False, "--all", "-a", help="Include archived.")) -> None:
    """List memories (name, type, description)."""
    memories = _store(workdir).load_all(include_archived=all)
    if not memories:
        info(console, "no memories yet. rocky remembers via the `remember` tool or /remember in chat.")
        return
    for m in memories:
        mark = "[dim]archived · [/dim]" if m.status == "archived" else ""
        console.print(f"  [bold]{m.name}[/bold] [dim]({mark}{m.type})[/dim] — {m.description}")


@memory_app.command("show")
def memory_show(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Print one memory in full (frontmatter + body)."""
    mem = _store(workdir).get(name)
    if mem is None or mem.path is None:
        fail(console, f"no memory named '{name}'.")
        raise typer.Exit(1)
    console.print(f"[dim]{mem.path}[/dim]\n{mem.path.read_text()}")


@memory_app.command("rm")
def memory_rm(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Archive a memory (moved to archive/, never deleted)."""
    if _store(workdir).archive(name):
        info(console, f"archived '{name}' → .rockycode/memory/archive/")
    else:
        fail(console, f"no active memory named '{name}'.")
        raise typer.Exit(1)


@memory_app.command("search")
def memory_search(
    query: str,
    workdir: Optional[Path] = _WORKDIR_OPT,
    keyword: bool = typer.Option(False, "--keyword", help="Skip embeddings; plain substring search."),
) -> None:
    """Semantic search (Ollama embeddings + keyword hybrid); --keyword for substring only."""
    store = _store(workdir)
    hits = []
    if not keyword:
        from rockycode.memory.index import IndexUnavailable, MemoryIndex, search_sync
        try:
            hits = [m for m, _ in search_sync(MemoryIndex(store), query)]
        except IndexUnavailable as e:
            confused(console, f"semantic index unavailable ({e}); falling back to substring.")
    if not hits:
        hits = store.search(query)
    if not hits:
        confused(console, f"nothing matches '{query}'.")
        return
    for m in hits:
        console.print(f"  [bold]{m.name}[/bold] [dim]({m.type})[/dim] — {m.description}")


@memory_app.command("reindex")
def memory_reindex(
    workdir: Optional[Path] = _WORKDIR_OPT,
    force: bool = typer.Option(False, "--force", help="Re-embed everything, ignoring hashes."),
) -> None:
    """Rebuild index.db from the markdown files (it is always safe to delete)."""
    from rockycode.memory.index import IndexUnavailable, MemoryIndex, reindex_sync
    try:
        indexed, kept, removed = reindex_sync(MemoryIndex(_store(workdir)), force=force)
    except IndexUnavailable as e:
        fail(console, f"semantic index unavailable: {e}")
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — usually Ollama not running
        fail(console, f"reindex failed ({type(e).__name__}: {e}). is ollama running?")
        raise typer.Exit(1)
    info(console, f"indexed {indexed}, unchanged {kept}, removed {removed}")


@memory_app.command("edit")
def memory_edit(name: str, workdir: Optional[Path] = _WORKDIR_OPT) -> None:
    """Open a memory file in $EDITOR."""
    mem = _store(workdir).get(name)
    if mem is None or mem.path is None:
        fail(console, f"no memory named '{name}'.")
        raise typer.Exit(1)
    editor = os.getenv("EDITOR", "vi")
    subprocess.run([editor, str(mem.path)])


@app.command()
def bench(
    runner: str = typer.Option("raw", help="'raw' (single-shot) or 'rockycode' (harness, v1+)."),
    tasks: str = typer.Option("dev10", help="'dev10', 'verified', or path to a JSON list of instance IDs."),
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    limit: Optional[int] = typer.Option(None, help="Cap number of tasks."),
    run_id: Optional[str] = typer.Option(None, help="Run label. Defaults to runner-model-timestamp."),
    skip_score: bool = typer.Option(False, "--skip-score", help="Generate predictions only."),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt",
        help="System prompt file for the rockycode runner (default: built-in ROCKY_SYSTEM).",
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
        help="Enable DeepSeek thinking mode (env: ROCKYCODE_THINKING).",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
        help="Reasoning depth when thinking is on: high | xhigh | max (xhigh sends "
             "max on DeepSeek; env: ROCKYCODE_REASONING_EFFORT).",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 16384),
        "--max-tokens",
        help="Max output tokens per call; CoT counts toward this when thinking is on (env: ROCKYCODE_MAX_TOKENS).",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 1048576),
        "--context-window",
        help="Model context window in tokens (DeepSeek V4 = 1M). Soft reminder at "
             "50% (V4 degrades past the half); auto-compacts near full (env: "
             "ROCKYCODE_CONTEXT_WINDOW).",
    ),
    max_steps: int = typer.Option(
        _env_int("ROCKYCODE_MAX_STEPS", 50),
        "--max-steps",
        help="Step cap per task; budget warnings injected near the end (env: ROCKYCODE_MAX_STEPS).",
    ),
    token_budget: int = typer.Option(
        _env_int("ROCKYCODE_TOKEN_BUDGET", 0),
        "--token-budget",
        help="Max total prompt+completion tokens across all tasks. 0 = unlimited (env: ROCKYCODE_TOKEN_BUDGET).",
    ),
) -> None:
    """Run rockycode against a SWE-bench task set and report the score."""
    show_banner(console)

    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        fail(console, "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(1)

    if reasoning_effort not in {"high", "xhigh", "max"}:
        fail(console, f"invalid --reasoning-effort '{reasoning_effort}'. use high, xhigh, or max.")
        raise typer.Exit(1)

    # raw needs docker only for scoring; the rockycode harness always needs it
    # (the agent works inside the task containers).
    if runner == "rockycode" or not skip_score:
        _docker_preflight()

    instance_ids = _load_task_ids(tasks)
    if limit and instance_ids:
        instance_ids = instance_ids[:limit]

    count = len(instance_ids) if instance_ids else "all (500)"
    # task-set label goes into the predictions filename so dev10/test20
    # runs never overwrite each other
    task_label = tasks if tasks in ("dev10", "verified") else Path(tasks).stem
    info(console, f"runner={runner}  tasks={tasks}  count={count}")
    info(console, f"model={model}  thinking={thinking}  effort={reasoning_effort}  max_tokens={max_tokens}")
    if token_budget:
        info(console, f"token budget={token_budget:,} (stops when total prompt+completion tokens exceed this)")

    if runner == "raw":
        if prompt is not None:
            confused(console, "--prompt only affects the rockycode runner; raw uses its fixed template.")
        from rockycode.runners.raw import run as run_raw
        predictions_path = run_raw(
            model=model,
            instance_ids=instance_ids,
            console=console,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            task_label=task_label,
        )
    elif runner == "rockycode":
        system_prompt, prompt_name, prompt_sha = _load_prompt(prompt)
        info(console, f"prompt={prompt_name} [dim]sha {prompt_sha}[/dim]")
        from rockycode.runners.agent import run as run_agent
        predictions_path = run_agent(
            model=model,
            instance_ids=instance_ids,
            console=console,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            context_window=context_window,
            max_steps=max_steps,
            token_budget=token_budget,
            system_prompt=system_prompt,
            prompt_name=prompt_name,
            prompt_sha=prompt_sha,
            task_label=task_label,
        )
    else:
        fail(console, f"unknown runner: {runner}")
        raise typer.Exit(1)

    if skip_score:
        info(console, f"predictions saved to {predictions_path}. score skipped.")
        return

    if not run_id:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"{runner}-{model.replace('/', '-')}-{ts}"

    from rockycode.score import score
    score(predictions_path=predictions_path, instance_ids=None, run_id=run_id, console=console)


@app.command()
def goal(
    objective: Optional[str] = typer.Argument(None, help="What to accomplish autonomously (omit with --clean)."),
    model: Optional[str] = typer.Option(None, help="Model for the work turns (default: ROCKYCODE_MODEL)."),
    reviewer_model: Optional[str] = typer.Option(
        None, "--reviewer-model",
        help="Model for milestone review (default: same as --model). Set deepseek-v4-pro "
             "for stronger review — it costs more.",
    ),
    max_usd: Optional[float] = typer.Option(None, "--max-usd", help="Spend ceiling in your currency (default: recommended)."),
    max_hours: Optional[float] = typer.Option(None, "--max-hours", help="Wallclock limit in hours (default: recommended 8h)."),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="Total-token cap."),
    review_every: int = typer.Option(3, "--review-every", help="Milestone-review cadence, in turns."),
    yes: bool = typer.Option(False, "--yes", help="Auto-approve ask-tier commands (git push, sudo, installs) and network."),
    network: Optional[bool] = typer.Option(
        None, "--network/--no-network",
        help="Give the sandbox internet access. Default: OFF (offline) — safer "
             "unattended. If the objective looks like it needs network (pip/apt/"
             "download), rocky asks you up front. Pass --network to force it on.",
    ),
    workdir: Path = typer.Option(Path.cwd(), "--workdir", help="Project root."),
    clean: bool = typer.Option(False, "--clean", help="Prune leftover goal worktrees (keeps branches), then exit."),
    context_file: Optional[Path] = typer.Option(None, "--context-file", hidden=True),
    result_file: Optional[Path] = typer.Option(None, "--result-file", hidden=True),
) -> None:
    """Run rocky AUTONOMOUSLY toward an objective — on an isolated copy of the repo,
    in the sandbox, under a hard budget. Review the result as a git branch."""
    import asyncio
    import time as _time

    from openai import AsyncOpenAI

    from rockycode.config import load as load_config
    from rockycode.engine.budget import GoalBudget, recommended
    from rockycode.engine.goal import EngineDriver, GoalRunner, safe_bash_tool
    from rockycode.engine.loop import Engine
    from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
    from rockycode.engine.worktree import GoalWorkspace
    from rockycode.onboarding import run_setup
    from rockycode.pricing import UsageLedger

    if clean:
        from rockycode.engine.worktree import prune_goal_worktrees
        removed = prune_goal_worktrees(workdir)
        if removed:
            info(console, f"pruned {len(removed)} goal worktree(s) — branches kept:")
            for p in removed:
                console.print(f"  [dim]{p}[/]")
        else:
            info(console, "no leftover goal worktrees to prune.")
        # Goal logs are small step-summaries (not raw output), but pile up one per
        # run — drop ones older than 14 days, keep recent ones for review.
        log_dir = Path.home() / ".rockycode" / "goal-logs"
        old = 0
        if log_dir.exists():
            for lg in log_dir.glob("*.log"):
                try:
                    if _time.time() - lg.stat().st_mtime > 14 * 86400:
                        lg.unlink()
                        old += 1
                except OSError:
                    pass
        if old:
            info(console, f"removed {old} goal log(s) older than 14 days.")
        info(console, f"goal logs: {log_dir}  [dim](small; review with `cat`)[/]")
        raise typer.Exit(0)
    if not objective:
        fail(console, "give an objective to run, or --clean to prune old goal worktrees.")
        raise typer.Exit(1)
    goal_context = ""  # chat digest on a /goal handoff — seeds planning
    if context_file and context_file.exists():
        try:
            goal_context = context_file.read_text()[:8000]
        except OSError:
            pass

    run_setup(console)  # first run: paste-your-key, then continue
    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        fail(console, "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(1)
    reviewer_model = reviewer_model or model
    currency = load_config(workdir)["currency"]
    if max_usd is None and max_hours is None and max_tokens is None:
        budget = recommended(currency)
    else:
        budget = GoalBudget(
            max_usd=max_usd,
            max_seconds=max_hours * 3600 if max_hours is not None else None,
            max_tokens=max_tokens,
            currency=currency,
        )

    async def _run() -> None:
        from rockycode.engine.safety import network_intent, pre_scan

        slug = _time.strftime("%Y%m%d-%H%M%S")
        ws = GoalWorkspace.create(workdir.resolve(), slug)
        info(console, f"isolated workspace: {ws.path}" + (f"  ·  branch {ws.branch}" if ws.branch else "  (copy)"))
        # Persistent per-run log (plan + every step + verify reasons) so the run is
        # reviewable after it scrolls away — the pointer is printed at the end and
        # surfaced when a chat hands off and resumes.
        log_dir = Path.home() / ".rockycode" / "goal-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{slug}.log"

        def _log(msg: str) -> None:
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                pass
        _log(f"# goal: {objective}\n# {slug}  workspace={ws.path}  branch={ws.branch}")
        ledger = UsageLedger()
        for w in project_env_warnings(Path.cwd()):
            info(console, w)
        client = AsyncOpenAI(api_key=require_key(), base_url=require_base_url(),
                             max_retries=5, timeout=300.0)
        # Plan BEFORE the sandbox exists (planning is LLM-only), so network/permits
        # are decided from the REAL plan, not a guess at your wording. Engine is
        # attached after the pre-flight decision.
        from rockycode.engine.explore import make_goal_verifier
        driver = EngineDriver(client=client, model=model, reviewer_model=reviewer_model,
                              workspace=ws, ledger=ledger, currency=currency,
                              verifier=make_goal_verifier(client=client, model=model,
                                                          workdir=ws.path, ledger=ledger))

        # Plan → derive permits → confirm, in a REFINE loop (all before the sandbox
        # exists, so it's cheap to iterate). At the gate: 'y' proceed, 'e' edit
        # (give guidance, re-plan, re-confirm), 'n' cancel. Proceeding grants the
        # plan's permits; --network/--no-network force the net setting; --yes auto.
        # Initial plan against the real files (folding in any chat-handoff context).
        plan_input = objective if not goal_context else (
            f"{objective}\n\n[Context from the chat that led here — use it to inform "
            f"the plan; the objective above is the goal]:\n{goal_context}")
        info(console, f"planning: {objective}")
        try:
            plan, requires = await driver.plan(plan_input)
        except Exception as e:  # noqa: BLE001
            fail(console, f"planning failed — {e}"); ws.cleanup(keep=False); raise typer.Exit(1)
        if not plan:
            fail(console, "the planner produced no milestones."); ws.cleanup(keep=False); raise typer.Exit(1)

        # Confirm loop: show plan → derive permits → gate. 'e' opens a real
        # back-and-forth — rocky ANSWERS your question, then shows the (revised or
        # same) plan — all before the sandbox exists, so iterating is cheap.
        while True:
            for i, m in enumerate(plan, 1):
                console.print(f"  [dim]{i}.[/] {m}")

            # derive needs FROM THE PLAN — the planner's REQUIRES line + a scan backstop
            scan_text = "\n".join(plan) + (("\n" + requires) if requires else "")
            flags = pre_scan(scan_text)
            blocked = [v for v in flags if v.action == "block"]
            if blocked:
                fail(console, f"plan names a blocked action: {blocked[0].reason}")
                ws.cleanup(keep=False); raise typer.Exit(1)
            asks = [v for v in flags if v.action == "ask"]
            net_reason = network_intent(requires) or network_intent(scan_text)
            use_network = network if network is not None else bool(net_reason)
            approved: set[str] = {v.pattern for v in asks}
            if use_network or asks or net_reason:
                info(console, "this run will need — approve before you leave:")
                if use_network:
                    console.print(f"  [magenta]🌐 network[/] — {net_reason or 'requested'}")
                elif net_reason:
                    console.print(f"  [dim]· plan implies network ({net_reason}) but --no-network is set — those steps will fail[/]")
                for v in asks:
                    console.print(f"  [yellow]⬆ {v.reason}[/]")
            else:
                info(console, "this run needs no extra permissions (offline, no privileged commands).")

            if yes:
                break
            # Explicit verbs so 'n' can't be misread as "no, let's talk" — that's 'e'.
            GO, EDIT, CANCEL = ("y", "yes", ""), ("e", "edit", "discuss"), ("n", "no", "c", "cancel")
            valid = GO + EDIT + CANCEL
            action = "?"
            while action not in valid:
                action = typer.prompt(
                    "  run this plan?  [y] yes (enter)  ·  [e] discuss/edit  ·  [n] cancel",
                    default="y", show_default=False).strip().lower()
                if action not in valid:
                    console.print("  [dim](y = run it · e = discuss/edit the plan · n = cancel)[/]")
            if action in GO:
                break
            if action in CANCEL:
                info(console, "cancelled — nothing ran. [dim](tip: 'e' discusses/edits the plan instead of cancelling)[/]")
                ws.cleanup(keep=False)
                raise typer.Exit(0)
            # edit → discuss: rocky answers, then re-shows the (possibly revised) plan
            msg = typer.prompt("  ask about or change the plan").strip()
            if msg:
                try:
                    reply, plan, requires = await driver.discuss(objective, plan, requires, msg)
                except Exception as e:  # noqa: BLE001
                    reply = f"(couldn't reason about that — {type(e).__name__})"
                if reply:
                    console.print(f"\n  [#8b6fc9]rocky:[/] [italic]{reply}[/]\n")
            # loop → re-show the (possibly revised) plan + re-gate
        info(console, f"sandbox network: {'ON' if use_network else 'off (offline)'}")
        _log("plan:\n" + "\n".join(f"  {i}. {m}" for i, m in enumerate(plan, 1)))

        # 4) NOW provision the sandbox with that decision and attach the engine
        try:
            sandbox = await ChatSandbox.start(ws.path, network=use_network)
        except Exception as e:  # noqa: BLE001
            fail(console, f"goal mode needs the sandbox (Docker) — {e}")
            ws.cleanup(keep=False)
            raise typer.Exit(1)
        reg = build_sandbox_registry(sandbox)
        reg["bash"] = safe_bash_tool(sandbox, approved)  # approvals frozen up front
        engine = Engine(model=model, client=client, workdir=ws.path, registry=reg,
                        trajectory_meta={"goal": objective, "runner": "goal"})
        driver.attach(engine, network=use_network)

        # 5) run the loop with the pre-computed plan (tee events to console + log)
        def _on_event(m: str) -> None:
            console.print(f"[dim]· {m}[/]")
            _log(m)
        runner = GoalRunner(objective, driver, budget, ws, ledger, review_every=review_every,
                            on_event=_on_event, preplanned=plan)
        info(console, f"budget — {budget.preflight_note()}")
        try:
            result = await runner.run()
        finally:
            await sandbox.stop()

        sym = "¥" if currency == "cny" else "$"
        info(console, f"goal {result.status}: {result.reason}")
        info(console, f"{result.milestones_done}/{result.milestones_total} milestones · "
                      f"spend {sym}{ledger.cost(currency):.4f}")
        if ws.branch:
            # Work is committed on the goal branch; the worktree dir is kept so
            # you can run/poke the files. Branch is never auto-deleted.
            info(console, f"review:  git -C {ws.origin} diff {ws.base or 'HEAD'}..{ws.branch}   (merge if good)")
            console.print(f"  run it:  cd {ws.path}")
            console.print(f"  tidy up: git -C {ws.origin} worktree remove {ws.path}   (keeps branch {ws.branch})")
        else:
            info(console, f"review the work in: {ws.path}")
        _log(f"result: {result.status} — {result.reason}  "
             f"({result.milestones_done}/{result.milestones_total} milestones)")
        info(console, f"log:   {log_path}   [dim](full plan + per-step verify — check it anytime)[/]")
        if result_file is not None:  # a /goal handoff reads this to resume chat
            import json as _json
            try:
                result_file.write_text(_json.dumps({
                    "status": result.status, "reason": result.reason,
                    "branch": ws.branch, "origin": str(ws.origin),
                    "milestones_done": result.milestones_done,
                    "milestones_total": result.milestones_total, "log": str(log_path),
                }))
            except OSError:
                pass

    asyncio.run(_run())


@app.command()
def serve(
    model: Optional[str] = typer.Option(None, help="Model ID. Defaults to ROCKYCODE_MODEL env."),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-C", help="Project directory (default: cwd)."
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 16384), "--max-tokens",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 131072), "--context-window",
    ),
    max_steps: int = typer.Option(
        _env_int("ROCKYCODE_CHAT_MAX_STEPS", 0), "--max-steps",
    ),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt",
        help="System prompt file (default: built-in ROCKY_SYSTEM).",
    ),
) -> None:
    """Start Rocky as a JSON-RPC 2.0 server over stdin/stdout (for editor clients)."""
    import asyncio

    from rockycode.engine.server import run_server

    model = model or os.getenv("ROCKYCODE_MODEL")
    if not model:
        # stdout is the JSON-RPC channel — a Rich error there corrupts the very
        # first bytes the client reads. Startup errors go to stderr.
        fail(Console(stderr=True), "no model. pass --model or set ROCKYCODE_MODEL in .env.")
        raise typer.Exit(1)

    workdir = (workdir or Path.cwd()).resolve()

    # Load custom system prompt if provided
    system_prompt = os.getenv("ROCKYCODE_SYSTEM_PROMPT", "")
    if prompt:
        system_prompt = prompt.read_text()

    asyncio.run(run_server(
        model=model, workdir=workdir,
        thinking=thinking, reasoning_effort=reasoning_effort,
        max_tokens=max_tokens, context_window=context_window,
        max_steps=max_steps, system_prompt=system_prompt,
    ))


if __name__ == "__main__":
    app()
