"""Slash commands — one table drives /help, the input's completion and dispatch.

Add a command here and it appears everywhere at once. A handler takes
(app, text); one that starts a Textual worker (goal, sandbox, paste, the
proposal and routine inboxes) returns the worker and is not awaited. A
command with a `when` predicate is listed (help, completion) only while it
holds; typed in full it still runs.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Optional

from rich.markup import escape
from textual.widgets import Static

from rockycode.engine.effort import EFFORT_LEVELS
from rockycode.palette import AMBER, LAVENDER, VIOLET
from rockycode.tui.widgets import TextViewerScreen

Handler = Callable[[object, str], object]


@dataclass(frozen=True)
class Command:
    name: str                       # "/plan"
    handler: Handler
    help: Optional[str] = None      # i18n key of its /help line (None = none of its own)
    aliases: tuple[str, ...] = ()
    when: Optional[Callable[[object], bool]] = None  # listed only while this holds (None = always)


def _say(app, text: str, cls: str = "tool-line"):
    return app._add(Static(text, classes=cls))


# ---- handlers (self → app) ---------------------------------------------------------

async def cmd_help(app, text: str) -> None:
    card = Static(help_text(app), classes="tool-line")
    await app._add(card)
    # Open at the first line: the card is taller than most terminals, and the
    # chat's pin-to-bottom used to show its tail (the least used commands).
    app.call_after_refresh(lambda: app._transcript().scroll_to_widget(card, top=True, animate=False))


async def cmd_task(app, text: str) -> None:
    from rockycode.tui.tasks import command
    await command(app, text)


async def cmd_exit(app, text: str) -> None:
    app._request_exit()


async def cmd_clear(app, text: str) -> None:
    await app._transcript().remove_children()
    # The screen is empty, the conversation is not: say so, and name the real fresh start.
    await _say(app, app._t("commands.cleared"))


async def cmd_queue(app, text: str) -> None:
    """Messages sent while rocky works: `on` = they wait their turn, `off` =
    they interrupt (the default); bare = the waiting ones; resume / clear."""
    action = text.partition(" ")[2].strip().lower()
    if action in ("on", "off"):
        app._delivery_mode = "queue" if action == "on" else "interrupt"
        app._render_status()
    elif action == "resume":
        app._waiting_external = False
        await app._delivery.resume()
    elif action == "clear":
        app._waiting_external = False
        await app._delivery.clear()
        app._unsent_pickups()  # a dropped "run it now" allowed nothing: its step waits again
    elif action:
        await _say(app, app._t("app.queue_usage"))
        return
    state = app._t("app.queue_state_paused" if app._delivery.paused else "app.queue_state_ready")
    lines = [app._t("app.queue_summary", mode=app._delivery_mode, count=len(app._delivery.pending), state=state)]
    lines.extend(f"{i}. {item.prompt[:120]}" for i, item in enumerate(app._delivery.pending, 1))
    await _say(app, escape("\n".join(lines)))


async def cmd_mode(app, text: str) -> None:
    family = text.split()[0].lower()[1:]  # research | learn
    await app._handle_mode_cmd(family, text)


async def cmd_config(app, text: str) -> None:
    from rockycode.config import DEFAULTS, GLOBAL_PATH, change_feedback, describe_value, set_value
    from rockycode.dream import reflect as reflect_mod
    parts = text.split(maxsplit=2)
    if len(parts) >= 3:  # /config key value  → set
        v, err = set_value(parts[1], parts[2], locale=app._language)
        if err:
            await _say(app, f"[{AMBER}]? {escape(err)}[/]")
            return
        # Model limits take effect on the live engine at once; other keys
        # (currency/theme/language) are read at startup.
        effective = describe_value(parts[1], app.engine.workdir)
        if effective["source"] == "project":
            note = change_feedback(parts[1], app.engine.workdir, app._language)
        elif parts[1] in ("context_window", "max_tokens"):
            # a number pins the limit across /model switches; 0 unpins it →
            # back to the active model's registry value
            app.engine.set_limit(parts[1], v)
            note = (app._t('commands.limit_pinned') if v
                    else app._t('commands.limit_model', p0=getattr(app.engine, parts[1])))
        elif parts[1] == "evolve":
            app._evolve = v
            if v == "auto":
                reflect_mod.attach_from_config(app.engine, app.engine.workdir)
            else:
                reflect_mod.detach(app.engine)
            note = app._t("commands.applied")
        elif parts[1] == "image":
            # Re-read from disk at every paste/view_image — a false "restart to
            # apply" here sends new users on a pointless restart right at the
            # end of image setup. A pinned endpoint nobody keyed is said now,
            # not discovered at the first paste.
            from rockycode.engine import vision
            setting = vision.image_setting(v)
            note = app._t('commands.each_use')
            if setting.route == "provider" and vision.sidecar_choice(setting.preferred, strict=True) is None:
                note += " " + app._t('commands.image_unmatched', p1=escape(str(v)))
        elif parts[1] == "artifact_theme":
            note = app._t('commands.each_use')  # read when an artifact is created; saved ones keep their look
        elif parts[1] == "model":
            note = app._t('commands.model_next_launch')
        elif parts[1] == "vision_models":
            note = app._t('commands.vision_next_switch')
        else:
            note = app._t('commands.restart')
        await _say(app, app._t('commands.config_saved', p0=VIOLET, p1=escape(parts[1]),
                               p2=escape(str(v)), p3=escape(note)))
        return
    # /config  → show
    lines = [app._t('commands.config_title', p0=VIOLET, p1=escape(str(GLOBAL_PATH)))]
    for k in DEFAULTS:
        detail = describe_value(k, app.engine.workdir)
        source = app._t("commands.source_" + detail["source"])
        origin = f"{source}: {escape(str(detail['path']))}" if detail["path"] else source
        lines.append(f"  [{LAVENDER}]{k}[/] = {escape(str(detail['value']))} [dim]({origin})[/]")
    lines.append(app._t('commands.config_usage'))
    await _say(app, "\n".join(lines))


async def cmd_prompt(app, text: str) -> None:
    await app.push_screen(TextViewerScreen(app._t('commands.prompt_title'), app.engine.history[0]["content"]))


async def cmd_skills(app, text: str) -> None:
    skills = getattr(app.engine, "skills", []) or []
    if not skills:
        await _say(app, app._t('commands.skills_empty'))
        return
    lines = [app._t('commands.skills_title')]
    lines += [f"  [{LAVENDER}]{s.name}[/] — {s.description} [dim]({s.source})[/]" for s in skills]
    await _say(app, "\n".join(lines))


async def cmd_memory(app, text: str) -> None:
    store = getattr(app.engine, "memory_store", None)
    memories = store.load_all() if store is not None else []
    if store is None:
        await _say(app, app._t('commands.memory_off'))
    elif not memories:
        await _say(app, app._t('commands.memory_empty'))
    else:
        lines = [app._t('commands.memory_title')]
        lines += [f"  [{LAVENDER}]{m.name}[/] [dim]({m.type})[/dim] — {m.description}" for m in memories]
        lines.append(app._t('commands.memory_usage'))
        await _say(app, "\n".join(lines))


async def cmd_remember(app, text: str) -> None:
    store = getattr(app.engine, "memory_store", None)
    note = text[len("/remember"):].strip()
    if store is None:
        await _say(app, app._t('commands.memory_off'))
    elif not note:
        await _say(app, app._t('commands.remember_usage'))
    else:
        from rockycode.memory import Memory
        mem = Memory(name="", type="feedback", description=note[:80], body=note, origin="user")
        path = store.save(mem)
        await _say(app, app._t('commands.remember_saved', p1=note, p2=path))


async def cmd_lsp(app, text: str) -> None:
    mgr = getattr(app.engine, "lsp_manager", None)
    problem = getattr(getattr(app.engine, "runtime", None), "lsp_error", None)
    if not getattr(app.engine, "lsp_enabled", True):
        await _say(app, app._t('commands.lsp_off'))
    elif problem:  # a bad command line, a refused start, a server that would not start
        await _say(app, app._t('commands.lsp_problem', p0=escape(str(problem))))
    elif mgr is None:  # on, but nothing names a server: say how, instead of "off"
        await _say(app, app._t('commands.lsp_unconfigured'))
    elif not mgr.is_running:
        await _say(app, app._t('commands.lsp_disconnected', p0=mgr.command))
    else:
        tools = ", ".join(sorted(mgr.available_tools)) or app._t("commands.listing")
        await _say(app, app._t('commands.lsp_connected', p2=mgr.command, p3=tools))


async def cmd_mcp(app, text: str) -> None:
    mgr = getattr(app.engine, "mcp_manager", None)
    if mgr is None:
        await _say(app, app._t('commands.mcp_off'))
        return
    lines = [f"[bold {VIOLET}]mcp[/]"]
    lines += [f"  {line}" for line in mgr.status()]
    tools = sorted(mgr.tools())
    if tools:
        lines.append(f"  [dim]{' · '.join(tools)}[/]")
    await _say(app, "\n".join(lines))


async def cmd_effort(app, text: str) -> None:
    parts = text.split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg in EFFORT_LEVELS:
        if arg == "off":
            app.engine.thinking = False
        else:
            app.engine.thinking = True
            app.engine.reasoning_effort = arg
        await _say(app, app._t('commands.effort_applied', p1=app._effort_note()))
    else:
        await _say(app, app._t('commands.effort_help', p1=app._effort_note()))


async def cmd_permission(app, text: str) -> None:
    parts = text.split()
    if len(parts) >= 2 and parts[1].lower() in ("yolo", "ask", "careful"):
        await app._apply_permission_switch(parts[1].lower())
    else:
        # Bare (or a typo'd mode) → the picker, same as clicking the chip: it
        # shows the three modes and what each one allows.
        app.open_permission_picker()


async def cmd_parked(app, text: str) -> None:
    app.open_parked()  # the typed twin of clicking the chip


# ---- the table: its order is the /help order ---------------------------------------

def _inbox(kind: str) -> Callable[[object], bool]:
    """The evolve inboxes earn a help line while evolve is on, or while that
    inbox has something waiting (counted once at launch)."""
    def when(app) -> bool:
        inbox = getattr(app, "_inbox", None) or {}
        return getattr(app, "_evolve", "off") == "auto" or bool(inbox.get(kind))
    return when


COMMANDS: tuple[Command, ...] = (
    Command("/help", cmd_help, "help.help"),
    Command("/task", cmd_task, "help.task"),
    Command("/plan", lambda app, text: app._handle_plan(text), "help.plan"),
    Command("/goal", lambda app, text: app._handle_goal(text), "help.goal"),       # worker: probes Docker off the pump
    Command("/loop", lambda app, text: app._handle_loop(text), "help.loop"),
    Command("/research", cmd_mode, "help.research"),
    Command("/learn", cmd_mode, "help.learn"),
    Command("/model", lambda app, text: app._handle_model(text), "help.model"),
    Command("/paste", lambda app, text: app._paste_worker(text_fallback=False), "help.paste"),  # worker
    Command("/sandbox", lambda app, text: app._handle_sandbox(text), "help.sandbox"),           # worker
    Command("/lsp", cmd_lsp, "help.lsp"),
    Command("/artifact", lambda app, text: app._handle_artifact(text), "help.artifact"),
    Command("/prompt", cmd_prompt, "help.prompt"),
    Command("/mcp", cmd_mcp, "help.mcp"),
    Command("/skills", cmd_skills, "help.skills"),
    Command("/memory", cmd_memory, "help.memory"),
    Command("/proposals", lambda app, text: app._review_proposals(), "help.proposals",
            when=_inbox("proposals")),  # worker
    Command("/routines", lambda app, text: app._review_routines(), "help.routines",
            when=_inbox("routines")),   # worker
    Command("/remember", cmd_remember, "help.remember"),
    Command("/config", cmd_config, "help.config"),
    Command("/effort", cmd_effort, "help.effort"),
    Command("/permission", cmd_permission, "app.permission_help"),
    Command("/parked", cmd_parked, "help.parked", when=lambda app: bool(getattr(getattr(app, "_approver", None), "parked", None))),
    Command("/clear", cmd_clear, "help.clear"),
    Command("/exit", cmd_exit, "help.exit"),
    Command("/queue", cmd_queue, "help.queue"),
)

_BY_NAME: dict[str, Command] = {}
for _c in COMMANDS:
    _BY_NAME[_c.name] = _c
    for _a in _c.aliases:
        _BY_NAME[_a] = _c


def lookup(token: str) -> Optional[Command]:
    return _BY_NAME.get(token.lower())


def shown(app=None) -> list[Command]:
    """The commands listed for this app (no app = the whole table)."""
    return [c for c in COMMANDS if app is None or c.when is None or c.when(app)]


def names(app=None) -> list[str]:
    """Every listed token, table order, aliases right after their command
    (what the input completes on)."""
    out: list[str] = []
    for c in shown(app):
        out.append(c.name)
        out.extend(c.aliases)
    return out


def help_text(app) -> str:
    """The /help card, built from the table in table order. `! <cmd>` is not a
    slash command, so its line is slotted after /permission by hand."""
    lines = [app._t("help.title")]
    for c in shown(app):
        if c.help:
            lines.append(app._t(c.help))
        if c.name == "/permission":
            lines.append(app._t("help.shell"))
    return "\n".join(lines)


async def dispatch(app, text: str) -> None:
    token = text.split()[0].lower()
    cmd = lookup(token)
    if cmd is None:
        await _say(app, app._t('commands.unknown', p1=token))
        return
    result = cmd.handler(app, text)
    if inspect.isawaitable(result):
        await result
