"""`rockycode chat` — the TUI, and the exit card it prints on the way out."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import typer

from rockycode.banner import fail, info

# Credentials and endpoint come ONLY from ~/.rockycode (.env / keychain) or an
# explicit shell export. Project .env files are deliberately never loaded — a
# repo must not be able to supply a key or redirect where the key is sent
# (project_env_warnings tells the user when one tries).
from rockycode.cli._common import (  # noqa: F401
    console, REPO_ROOT, _config_model, _docker_preflight, _env_bool, _env_int, _launch_model, _load_prompt,
    _load_task_ids,
)


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
        help="Enable web_search/web_research/web_fetch tools (native search runs on "
             "the registry's search model — deepseek-flash — via DeepSeek's Anthropic "
             "endpoint; env: ROCKYCODE_SEARCH_MODEL, ROCKYCODE_SEARCH_ORDER).",
    ),
    thinking: bool = typer.Option(
        _env_bool("ROCKYCODE_THINKING", True),
        "--thinking/--no-thinking",
        help="Enable the model's thinking mode (env: ROCKYCODE_THINKING). A model "
             "that can't switch it off gets its lowest effort tier instead.",
    ),
    reasoning_effort: str = typer.Option(
        os.getenv("ROCKYCODE_REASONING_EFFORT", "max"),
        "--reasoning-effort",
        help="Reasoning depth when thinking is on: low | high | max. The dial is "
             "provider-neutral — each provider's own tiers come from the model "
             "registry and the dial is clamped onto them at the wire (xhigh is "
             "still accepted and means max; env: ROCKYCODE_REASONING_EFFORT).",
    ),
    max_tokens: int = typer.Option(
        _env_int("ROCKYCODE_MAX_TOKENS", 0),
        "--max-tokens",
        help="Max output tokens per call, incl. thinking/CoT. 0 (default) = the "
             "active model's real cap from the registry (deepseek-flash: 384K) and "
             "follows /model switches; a number is your own "
             "ceiling (env: ROCKYCODE_MAX_TOKENS; config max_tokens).",
    ),
    context_window: int = typer.Option(
        _env_int("ROCKYCODE_CONTEXT_WINDOW", 0),
        "--context-window",
        help="Model context window in tokens. 0 (default) = the active model's "
             "registry value (most current models: 1M). Soft reminder at 50%; "
             "auto-compacts near full (env: ROCKYCODE_CONTEXT_WINDOW; config "
             "context_window).",
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
        help="Connect to an LSP-over-MCP server for code navigation (experimental; env: ROCKYCODE_LSP_COMMAND).",
    ),
    live: bool = typer.Option(
        os.getenv("ROCKYCODE_ARTIFACTS_LIVE", "").lower() in ("1", "true", "yes"),
        "--live/--no-live",
        help="Serve artifact previews locally with auto-refresh. TUI generation saves "
             "a card; open its preview explicitly. Default off. env: ROCKYCODE_ARTIFACTS_LIVE.",
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
             "number for a finite budget plus 10 closing calls. env: ROCKYCODE_CHAT_MAX_STEPS.",
    ),
) -> None:
    """Talk to rocky: the interactive agent TUI. amaze!"""
    model = _launch_model(model)  # first run: paste-your-key, then continue
    if reasoning_effort not in {"low", "high", "max", "xhigh"}:
        fail(console, f"invalid --reasoning-effort '{reasoning_effort}'. use low, high, or max.")
        raise typer.Exit(1)

    # Textual requests the kitty keyboard protocol (report-all-keys); iTerm2
    # honors it and then bypasses IME composition, so Chinese/Japanese input
    # never reaches the app (textual#6552). Legacy key reporting loses us
    # nothing — basic bindings only. Must be set before textual is imported;
    # export TEXTUAL_DISABLE_KITTY_KEY=0 to opt back in.
    os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

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

    from rockycode.runtime import build_runtime
    from rockycode.tui.tasks import TaskSwitch
    while True:
        sb = None
        if sandbox:
            import asyncio as _asyncio
            from rockycode.engine.sandbox import ChatSandbox
            info(console, "sandbox: starting container…")
            try:
                sb = _asyncio.run(ChatSandbox.start(wd, network=sandbox_network))
            except Exception as exc:
                fail(console, f"sandbox start failed: {exc}")
                raise typer.Exit(1)
        runtime = build_runtime(
            model=model, workdir=wd, source="chat", allowed_roots=allowed_roots,
            system_prompt=system_prompt, prompt_name=prompt_name, prompt_sha=prompt_sha,
            thinking=thinking, reasoning_effort=reasoning_effort, max_tokens=max_tokens,
            context_window=context_window, max_steps=max_steps, mcp=mcp, skills=skills,
            memory=memory, web=web, lsp=lsp, loops=True, live=live, sandbox=sb,
        )
        engine = runtime.engine
        for notice in runtime.notices:
            info(console, notice)

        # /goal now runs INSIDE the app in its own screen (plan → confirm → work →
        # summary), then pops back to chat — no exit, no bare terminal, no subprocess.
        switched = run_app(
            engine, resume=resume_pick, resume_session=resume_info, sandbox=sb,
            currency=cfg["currency"], theme=cfg["theme"], language=cfg["language"],
            ui_language=cfg["ui_language"],
            permission=perm, permission_weakened=perm_weakened,
            evolve=cfg["evolve"],
        )
        if not isinstance(switched, TaskSwitch):
            break
        resume_info, resume_pick = switched.info, False
        sandbox = switched.sandbox
        perm, perm_weakened = switched.permission, False
    engine.finalize_outcome()  # idempotent — the app's exit flow normally beat us to it
    from rockycode.i18n import resolve_locale
    _print_exit_card(engine, resolve_locale(cfg["ui_language"], cfg["language"]))


def _print_exit_card(engine, locale: str = "en") -> None:
    """The resume handoff: after the app closes, print the session's id, title,
    and folder plus the exact way back — full commands, long flags only (short
    flags are unmemorable; see the exit-card design in docs/resume-design.md).
    In the UI language: the card is the last thing a session says."""
    path = getattr(getattr(engine, "trajectory", None), "path", None)
    if path is None:
        return  # logging was disabled (unwritable store) — nothing to resume
    from rich.markup import escape

    from rockycode.i18n import tr
    from rockycode.session import _read_info, public_id
    s = _read_info(Path(path))
    if s is None or s.summary == "(no message)":
        return  # no user turn ever happened — nothing worth resuming
    sid = public_id(s.session_id)
    folder = Path(s.project_path).name if s.project_path else s.project_name
    console.print(tr("exit.saved", locale, sid=sid, title=escape(s.display_title), folder=folder, count=s.n_messages))
    console.print(tr("exit.resume", locale, sid=sid))
    console.print(tr("exit.browse", locale))
    n = getattr(engine, "loops_at_exit", 0)
    if n:
        console.print(tr("exit.loops", locale, count=n, s="s" if n != 1 and locale == "en" else ""))
