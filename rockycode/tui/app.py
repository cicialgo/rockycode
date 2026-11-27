"""The rockycode TUI shell.

Layout:  top bar (title + status word) / scrolling transcript / input.

Styling: a custom "rocky" theme with explicit hex colors (soft purples).
Never use ANSI named colors ("magenta", "cyan") in this app — terminals
remap them arbitrarily (neon pink, electric cyan) and it gets ugly fast.

Streaming discipline (this is the no-glitch contract):
- engine deltas land in string buffers, never directly in widgets
- a 10 Hz flush timer moves dirty buffers into widgets in one update each
- the transcript is a real scroll container — no terminal-scrollback
  arithmetic anywhere
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.theme import Theme
from textual.widgets import Collapsible, Markdown, Static
from textual.worker import Worker, WorkerCancelled, WorkerFailed, WorkerState

from rockycode.banner import ROCKY_TAGLINE
from rockycode.i18n import tr, resolve_locale
from rockycode.dream import reflect as reflect_mod
from rockycode.engine import AgentState, Engine
from rockycode.engine.approval import ToolApprover, tool_brief
from rockycode.engine import cron as cron_mod
from rockycode.engine import tools as tools_mod
from rockycode.engine.delivery import TurnDelivery
from rockycode.engine.providers import fmt_tokens
from rockycode.engine.events import (
    CacheReset,
    Compacted,
    ContextReminder,
    EngineError,
    StateChanged,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)

from rockycode.palette import AMBER, LAVENDER, MUTED, PURPLE, RED, VIOLET
from rockycode.onboarding import project_env_warnings
from rockycode.tui.approval import ApprovalMixin
from rockycode.tui.planflow import PlanMixin
from rockycode.tui.modeflow import ModeMixin
from rockycode.tui.evolve import EvolveMixin
from rockycode.tui.loops import LoopsMixin
from rockycode.tui.modelflow import ModelMixin
from rockycode.tui.artifacts import ArtifactsMixin
from rockycode.tui.goalflow import GoalMixin
from rockycode.tui.sandboxflow import SandboxMixin
from rockycode.tui.images import ImagesMixin
from rockycode.tui.docs import DocsMixin
from rockycode.tui.chatinput import ChatInput
from rockycode.tui import commands
from rockycode.tui.widgets import EditChip, ParkChip, PermChip
from rockycode.tui.notestrip import NOTES, NoteStrip
from rockycode.tui.mdterm import (
    enhance_markdown,
    rocky_markdown_parser,
)
from rockycode.tui.prompt_history import PromptHistory
from rockycode.tui.removal import gone

FLUSH_HZ = 10

ROCKY_THEME = Theme(
    name="rocky",
    primary=PURPLE,
    secondary=LAVENDER,
    accent=VIOLET,
    foreground="#c0caf5",
    background="#16161e",
    surface="#1e1e28",
    panel="#23232f",
    success=PURPLE,
    warning=AMBER,
    error=RED,
    dark=True,
)

ROCKY_THEME_LIGHT = Theme(
    name="rocky-light",
    primary=PURPLE,
    secondary="#5a5e7a",
    accent="#7c5cba",
    foreground="#2a2a38",
    background="#f6f4fb",
    surface="#ffffff",
    panel="#ece8f7",
    success="#6a4ca3",
    warning="#b5852f",
    error=RED,
    dark=False,
)


def _status_text(state: AgentState, note: str, language: str = "en") -> str:
    if state == AgentState.IDLE:
        return ""
    return tr("app.status." + state.name.lower(), language, note=note)




LOOP_HELP = tr("app.loop_help")


class RockyCodeApp(ApprovalMixin, PlanMixin, ModeMixin, EvolveMixin, LoopsMixin, ModelMixin, ArtifactsMixin, GoalMixin, SandboxMixin, ImagesMixin, DocsMixin, App):
    TITLE = "rockycode"

    CSS = """
    Screen { layout: vertical; }
    #topbar {
        dock: top;
        height: 4;
        border-bottom: solid $primary;
    }
    #title-block { width: 1fr; padding: 1 2; }
    #status { width: auto; padding: 1 2; content-align: right middle; }
    #workspace { height: 1fr; }
    #transcript {
        width: 1fr;
        height: 100%;
        margin: 1 1 0 1;
        padding: 0 2;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary 40%;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: $surface;
    }
    #docdock {
        width: 45%;
        height: 100%;
        margin: 1 1 0 0;
        border: round $primary;
        border-title-color: $text-muted;
    }
    #docdock-head { height: 1; padding: 0 1; }
    #docdock-body {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary 40%;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: $surface;
    }
    #prompt {
        margin: 0 1;
        padding: 0 1;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    #prompt:focus { border: round $primary; }
    #statusbar { height: 1; padding: 0 2; }
    #cwd { width: 1fr; color: $text-muted; }
    #editchip { width: auto; margin: 0 2; display: none; }
    #parkchip { width: auto; margin-right: 2; display: none; }
    #modechip { width: auto; margin-right: 2; }
    #permchip { width: auto; margin-right: 2; }
    #total { width: auto; color: $text-muted; content-align: right middle; }
    #hints { height: 1; padding: 0 2; color: $text-muted; }

    .user-msg { margin: 1 0 0 0; }
    .rocky-label { margin: 1 0 0 0; }
    .tool-line { color: $text-muted; }
    .error-msg { color: $error; }
    .usage-line { color: $text-muted; text-style: italic; margin: 0 0 1 0; }
    .thinking-body { color: $text-muted; text-style: italic; }
    Collapsible { margin: 0; border: none; }
    Markdown { margin: 0 0 1 0; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "quit"),
        ("escape", "cancel_turn", "stop turn"),
        # priority: the focused input (a TextArea) would otherwise eat these
        # for its own cursor, so the transcript could never scroll.
        Binding("pageup", "transcript_up", "scroll up", priority=True),
        Binding("pagedown", "transcript_down", "scroll down", priority=True),
        Binding("shift+up", "transcript_line_up", "scroll up", priority=True),
        Binding("shift+down", "transcript_line_down", "scroll down", priority=True),
        # shift+tab steps the approval mode one notch looser (careful → ask →
        # yolo → careful). priority: the focused input and the approval card
        # would otherwise eat it, and Textual's own screen binding would move
        # focus instead. It is never the ONLY way in — the status-bar chip
        # spells the key out and opens the picker on click.
        Binding("shift+tab", "cycle_permission", "approval mode", priority=True),
    ]

    def __init__(
        self, engine: Engine, *, resume: bool = False, resume_session=None, sandbox=None,
        currency: str = "usd", theme: str = "dark", language: str = "auto", ui_language: str = "auto",
        permission: str = "ask", permission_weakened: bool = False,
        prompt_history: PromptHistory | None = None,
        evolve: str = "auto",
    ) -> None:
        super().__init__()
        self.engine = engine
        self.engine.enable_planning()
        self._prompt_history = prompt_history
        self._resume_requested = resume
        self._resume_session = resume_session  # SessionInfo → skip the picker
        self._titled = False  # one title record per session file (see _maybe_title)
        self._currency = currency
        self._theme_pref = theme
        # UI locale is independent of response language. Auto preserves the
        # historical language=en/zh behavior, with English for auto/auto.
        self._language = resolve_locale(ui_language, language)
        self._permission_weakened = permission_weakened
        # The shared approval pipeline (engine/approval.py); this app is the
        # host that answers its `ask` with the inline card. The mode, session
        # grants, loop-tick state and the verifier all live on it.
        self._approver = ToolApprover(
            registry=engine.registry, workdir=engine.workdir, ask=self._ask_request,
            mode=permission, engine=engine, notify=self._on_approval_note,
            park=True)  # yolo never waits: a step only the user can allow goes to the waiting list (#parkchip)
        # "Run it now": the message that tells rocky to pick the steps up → those steps. They are
        # armed while that message's turn runs, and at no other time.
        self._pickups: dict[str, list] = {}
        self._parked_notes: list[str] = []  # "don't run it" decided mid-turn: told to rocky when the turn ends
        if getattr(engine, "file_access", None) is not None:
            engine.file_access.local_mode = lambda: self._permission_mode
        self._turn_worker: Worker | None = None  # captured so Esc can cancel it
        self._delivery_mode = "interrupt"
        self._delivery_worker: Worker | None = None
        self._delivery_failed = False
        self._waiting_external = False
        self._delivery = TurnDelivery(self._run_delivery, on_change=self._render_status)
        # Loops (/loop — engine/cron.py). The CLI creates the book when it
        # registers loop_start/loop_stop (before the app exists); a bare Engine
        # (tests, serve) gets one here. Session state only — nothing persists.
        from rockycode.engine.cron import LoopBook
        self._loops: LoopBook = getattr(engine, "loop_book", None) or LoopBook(currency=currency)
        engine.loop_book = self._loops
        self._cancelled_tick = None            # (loop, tick_no) for _after_cancel's line
        self._quiet_lines: dict[int, Static] = {}      # loop id → its in-place quiet-streak line
        self._loop_hinted = False              # the one-time "want a ceiling?" hint
        from rockycode.pricing import UsageLedger
        self._ledger = getattr(engine, "ledger", None) or UsageLedger()
        if getattr(engine, "provider_local", False):  # launched straight onto
            self._ledger.mark_free(engine.model)      # a local model → $0
        # context_window before the first LOCAL switch, restored when switching
        # back to a cloud provider (a /config context_window set clears it —
        # an explicit value is the user's own).
        self.sandbox = sandbox  # ChatSandbox | None — created on /sandbox on
        self._local_registry: dict | None = None  # saved so we can swap back
        self._local_file_access = getattr(engine, "file_access", None)
        self._local_file_access = self.engine.file_access
        self._sandbox_starting = False  # guard concurrent /sandbox on
        self._lsp_connecting = False  # guard concurrent LSP start
        # Preview is opt-in; saving always produces a card without opening a browser.
        self._artifact_live = bool(getattr(engine, "artifact_live", False))
        self._artifact_server = None  # ArtifactServer once live mode starts
        # Set by the plan gate's "approve": the implement-turn text, submitted
        # from on_worker_state_changed once the current plan-mode turn finishes.
        self._plan_go: str | None = None
        # Evolve v2: "auto" | "off" (config; live via /config). Gates the
        # launch catch-up dream AND the exit-time final reflect below.
        self._evolve = evolve
        # Launch-time inbox counts (proposals waiting, routines due) — they keep
        # /proposals and /routines listed while evolve is off and something waits.
        self._inbox: dict[str, int] = {}
        self._exiting = False  # re-entry guard: a 2nd /exit or ctrl+q leaves NOW; nothing new starts once set
        self._final_reflect = None  # the exit reflect while it runs (see _exit_with_reflect)
        self._left = False  # exit() was called: Textual refuses every mount from then on
        self._switch_target = None  # a TaskSwitch set by /task open; the exit flow returns it to the CLI loop
        # Image attachments pending for the NEXT message: chip number → file
        # path. A paste/attach inserts "[image #N]" into the input; submit sends
        # the images whose chip text survived (deleting a chip detaches it).
        self._pending_images: dict[int, str] = {}
        self._img_seq = 0  # session-wide chip counter, so numbers never reuse

    # animate=False on purpose: instant, snappy scroll (the default animation
    # lags on a fast keypress and, in headless pilot runs, hadn't applied yet).
    def action_transcript_up(self) -> None:
        from rockycode.tui.plangate import InlinePlanGate
        if self.query(InlinePlanGate):
            self.query_one(InlinePlanGate).action_scroll_plan(-1)
            return
        self._transcript().scroll_page_up(animate=False)

    def action_transcript_down(self) -> None:
        from rockycode.tui.plangate import InlinePlanGate
        if self.query(InlinePlanGate):
            self.query_one(InlinePlanGate).action_scroll_plan(1)
            return
        self._transcript().scroll_page_down(animate=False)

    def action_transcript_line_up(self) -> None:
        self._transcript().scroll_up(animate=False)

    def action_transcript_line_down(self) -> None:
        self._transcript().scroll_down(animate=False)

    # ---- permission + interrupt ---------------------------------------------

    async def action_cancel_turn(self) -> None:
        """Esc: interrupt the running turn. Cancelling the worker raises
        CancelledError into run_turn, whose finally backfills tool responses so
        history stays API-valid; on_worker_state_changed resets the status.
        With no turn running, esc closes the doc dock instead (interrupting
        always wins — while streaming, close the dock with its ✕)."""
        from rockycode.tui.plangate import InlinePlanGate
        if self.query(InlinePlanGate):
            self.query_one(InlinePlanGate).action_pick("dismiss")
            return
        w = self._turn_worker
        if self._delivery.running:
            self.run_worker(self._delivery.stop(), group="delivery-control", exclusive=False)
            return
        if w is not None and w.is_running:
            self._waiting_external = False
            w.cancel()
            return
        self.action_doc_close()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # The delivery runner awaits and finalizes its own worker before the
        # next turn starts. An event handler must not race that cleanup.
        if event.worker is self._delivery_worker:
            return
        # A binding-Esc cancel raises CancelledError (BaseException), which the
        # _run_turn wrapper deliberately does NOT catch, so the worker ends
        # CANCELLED. The `is self._turn_worker` guard also excludes the prior
        # worker that @work(exclusive) cancels on a new submit (its handle was
        # already replaced), so this fires only for an explicit interrupt.
        if event.worker is self._turn_worker and event.state is WorkerState.CANCELLED:
            self._set_status(AgentState.IDLE)
            # The cancelled worker can't await; finalize from a fresh one so the
            # partial reply renders as markdown (matching the modal-cancel path).
            self.run_worker(self._after_cancel(), exclusive=False)
        # Plan gate "approve": the implement turn is submitted only AFTER the
        # plan-mode turn finishes (it can't self-submit from inside its own worker).
        elif (event.worker is self._turn_worker and not self._exiting
              and event.state is WorkerState.SUCCESS and self._plan_go):
            text, self._plan_go = self._plan_go, None
            self._turn_worker = self._run_turn(text)
        elif event.worker is self._turn_worker and self._waiting_external:
            if event.state is WorkerState.SUCCESS:
                self._waiting_external = False
                if not self._delivery_failed:
                    self.run_worker(self._delivery.resume(), exclusive=False)
            elif event.state in (WorkerState.CANCELLED, WorkerState.ERROR):
                self._waiting_external = False

    async def _after_cancel(self) -> None:
        await self._flush_buffers()
        await self._finalize_reply()
        ct, self._cancelled_tick = self._cancelled_tick, None
        if ct is not None:  # Esc landed on a loop tick — the loop itself lives on
            loop, tick_no = ct
            await self._add(Static(
                self._t('app.loop_tick_cancelled', p1=loop.id, p2=tick_no, p3=cron_mod.fmt_duration(loop.interval_s)), classes="tool-line"))
        else:
            await self._add(Static(self._t('app.turn_cancelled'), classes="tool-line"))
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort during teardown
            pass

    # ---- approval: the host side of engine/approval.py ----------------------
    # The pipeline (static policy → session grants → loop-tick rules → verifier
    # → ask) lives on self._approver. These properties are the names the rest
    # of the app (and its tests) use for that state.

    def _t(self, key: str, **params) -> str:
        return tr(key, self._language, **params)

    # ---- plan mode ----------------------------------------------------------

    def _render_cwd(self) -> None:
        plan = self._t('app.plan') if self.engine.plan_file is not None else ""
        registry = getattr(self.engine, "artifact_registry", None)
        artifacts = ""
        count = registry.count if registry is not None else 0  # read once: it asks the store
        if count:
            opened = registry.open_clients
            open_note = self._t('app.open', p0=opened) if opened else ""
            plural = "" if count == 1 or self._language == "zh" else "s"
            artifacts = self._t('app.artifact', p1=count, p2=plural, p3=open_note)
        # Loops: always visible while any is live — a loop spends while you're
        # away (same reasoning as the yolo chip). A clock while a tick runs.
        loops = ""
        live = self._loops.live()
        if self._tick_loop is not None:
            loops = (self._t('app.loop_ticking', p1=cron_mod.clock_face(datetime.now()), p2=self._tick_loop.id))
        elif live:
            nxt = self._loops.next_due()
            when = self._t('app.next', p0=datetime.fromtimestamp(nxt.next_due)) if nxt else self._t('app.paused')
            loops = (self._t('app.loop', p1=len(live), p2='s' if len(live) != 1 and self._language == 'en' else '', p3=self._sym(), p4=sum((lp.spend for lp in live)), p5=when))
        try:
            self.query_one("#cwd", Static).update(
                f"[{MUTED}]📁 {self.engine.workdir.name}[/]{plan}{artifacts}{loops}"
            )
        except NoMatches:
            pass  # a screen is on top of chat — the badge isn't visible now
        self._render_perm_chip()

    def on_mouse_scroll_up(self, event) -> None:
        self._transcript().scroll_relative(y=-3, animate=False)

    def on_mouse_scroll_down(self, event) -> None:
        self._transcript().scroll_relative(y=3, animate=False)

    def on_mouse_up(self, event) -> None:
        """Drag over transcript text, release → it's in the clipboard (OSC 52).
        The release IS the copy — no second action. Selection is cleared after
        copying so a later stray click can't silently re-clobber the clipboard.
        Plain clicks never have a selection, so links stay links; the input
        box's own TextArea selection is a separate mechanism, untouched."""
        text = self.screen.get_selected_text()
        if not text:
            return
        self.copy_to_clipboard(text)  # OSC 52 — remote/ssh terminals
        self._pbcopy(text)            # local macOS clipboard — works even where
        #                               the terminal blocks OSC 52 (iTerm2 default)
        self.clear_selection()
        n = len(text.splitlines())
        what = self._t('app.lines', p0=n) if n > 1 else (self._t('app.chars', p0=len(text)) if len(text) > 24 else repr(text))
        self.notify(self._t('app.copied', p0=what), timeout=2)

    def _pbcopy(self, text: str) -> bool:
        """Write the system clipboard directly (macOS pbcopy). iTerm2 ships
        with OSC-52 clipboard access OFF, so the escape-sequence path silently
        drops the copy there — the local pipe always lands."""
        import shutil
        import subprocess
        if shutil.which("pbcopy") is None:
            return False
        try:
            subprocess.run(["pbcopy"], input=text.encode(), timeout=2, check=True)
            return True
        except Exception:  # noqa: BLE001 — OSC 52 already attempted
            return False

    def _title_text(self) -> str:
        # Single source for the topbar title: compose() renders it once, and a
        # /model switch re-renders it — the two must never drift apart.
        return (f"[bold {VIOLET}]ROCKYCODE[/] [{MUTED}]· {self.engine.model}[/]\n"
                f"[italic {MUTED}]{ROCKY_TAGLINE}[/]")

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(self._title_text(), id="title-block")
            yield Static("", id="status")
        # This project's tasks as clickable notes; hidden until there are two.
        yield NoteStrip(id="notestrip")
        # #workspace: the doc dock mounts beside the transcript in here, so a
        # docked paper narrows the chat instead of covering it.
        with Horizontal(id="workspace"):
            transcript = VerticalScroll(id="transcript")
            # Non-focusable on purpose: clicking the transcript must never steal
            # focus from the input. Otherwise focus gets stuck on the scroll panel
            # and you can't type the next message — and in old iTerm2 click-to-
            # refocus the input is unreliable, so there's no way back. Scrolling
            # still works via the wheel (app-level handlers) and the pageup /
            # shift-arrow bindings, all of which work while the input stays focused.
            transcript.can_focus = False
            yield transcript
        prompt = ChatInput(id="prompt", history=self._prompt_history)
        prompt.border_title = self._t('app.ask_rocky')
        prompt.border_subtitle = self._t('app.input_keys')
        yield prompt
        with Horizontal(id="statusbar"):
            yield Static(f"[{MUTED}]📁 {self.engine.workdir.name}[/]", id="cwd")
            yield EditChip("", id="editchip")
            yield ParkChip("", id="parkchip")
            yield Static("", id="modechip")
            yield PermChip("", id="permchip")
            yield Static("", id="total")
        yield Static(self._t('app.hints_bar'), id="hints")

    async def on_mount(self) -> None:
        self.register_theme(ROCKY_THEME)
        self.register_theme(ROCKY_THEME_LIGHT)
        self.theme = "rocky-light" if self._theme_pref == "light" else "rocky"
        # Wire the interactive approver — always, even in yolo, so /permission can
        # flip modes mid-session; decide() short-circuits yolo to "allow" (no modal)
        # unless explicit shadow-all observation adds a bounded model call.
        # Headless/bench never set an approver, so they keep the
        # engine's zero-overhead always-allow default.
        self.engine.approver = self._approver
        # Running tool calls: call id → (its transcript line, its arguments). The
        # line a call starts with becomes its result line.
        self._tool_lines: dict[str, tuple[Static, dict]] = {}
        # Streaming buffers; the flush timer is the only writer to widgets.
        self._think_buf = ""
        self._reply_buf = ""
        self._think_flushed = 0
        self._reply_flushed = 0
        self._think_widget: Static | None = None
        self._think_box: Collapsible | None = None
        self._reply_widget: Markdown | None = None
        self._local_registry = dict(self.engine.registry)
        self._state = AgentState.IDLE
        self._note_i = 0
        self.set_interval(1 / FLUSH_HZ, self._flush_buffers)
        self.set_interval(0.4, self._animate_status)
        # Loops: rocky's loop_start/loop_stop render through the same receipt
        # path as /loop; the pump starts a due tick only when the chat is idle.
        self._loops.notify = self._on_loop_event
        self.set_interval(1.0, self._loop_pump)
        # Another terminal can create a task or leave mail; the strip catches up.
        self.set_interval(5.0, self.refresh_notes)
        self._transcript().border_title = self._t('app.chat')
        # Persistent permission reminder in the bottom bar — always visible, so a
        # no-prompt (yolo) session can't be forgotten mid-research.
        self._setup_artifacts()
        self._render_cwd()
        self._render_mode_chip()  # a launch-time config mode lights the chip
        if self._permission_weakened:
            warning = self._t("app.permission_weakened", mode=self._permission_mode)
            await self._add(Static(
                f"[bold {RED}]⚠ {warning}[/]", classes="error-msg"))
        # Inside the TUI, not the pre-launch console — the alt screen covers
        # anything printed before the app starts (found in cici's live test:
        # the warning was only visible after exit, i.e. exactly too late).
        for w in project_env_warnings(self.engine.workdir):
            await self._add(Static(f"[{AMBER}]· {escape(w)}[/]", classes="tool-line"))
        if os.environ.get("TERM_PROGRAM") == "iTerm.app":
            await self._add(Static(
                self._t('app.iterm2_mouse_hint'),
                classes="tool-line"))
        if self._resume_session is not None:
            await self._load_session(self._resume_session)  # direct: --resume <id>
            self.query_one(ChatInput).focus()
        elif self._resume_requested:
            self._resume_flow()  # picker → replay (or fall through to fresh)
        else:
            await self._greet()
        # If we're starting in yolo on the host, say what that means up front.
        await self._maybe_nudge_yolo_host()
        # Catch-up dream over past sessions (background; silent when idle-less).
        self._dream_catchup()
        await self._evolve_startup()

    async def _greet(self) -> None:
        from rockycode.tui.tasks import attach
        attach(self)
        await self._add(Static(self._t("app.welcome"), classes="rocky-label"))
        label = self._t("app.permission")
        hint = self._t("app.perm_switch")
        await self._add(Static(
            f"[dim]· {label}: {self._permission_note()}[/]\n[dim]  {hint}[/]", classes="tool-line"))
        if self._evolve == "auto" and reflect_mod.attached(self.engine) is not None:
            await self._maybe_evolve_disclosure()
        for fname in getattr(self.engine, "project_notes", []) or []:
            await self._add(Static(self._t('app.loaded_project_instructions', p0=fname), classes="tool-line"))
        n_skills = len(getattr(self.engine, "skills", []) or [])
        if n_skills:
            await self._add(
                Static(self._t('app.skill_s_available_skills_to_list', p0=n_skills), classes="tool-line")
            )
        if getattr(self.engine, "web_enabled", False):
            order = " → ".join(getattr(self.engine, "web_order", ()) or ())
            await self._add(Static(self._t('app.web_tools_on_search', p0=order), classes="tool-line"))
        if getattr(self.engine, "mcp_manager", None) and self.engine.mcp_manager.configs:
            names = ", ".join(self.engine.mcp_manager.configs)
            await self._add(Static(self._t('app.connecting_mcp_servers', p0=names), classes="tool-line"))
            self._connect_mcp()
        if getattr(self.engine, "lsp_manager", None) is not None:
            await self._add(Static(self._t('app.connecting_lsp'), classes="tool-line"))
            self._connect_lsp()
        # An explicit live preference lazily starts the preview server.
        self.engine.artifact_target = self._artifact_target

    @work
    async def _resume_flow(self) -> None:
        from rockycode.tui.resume import ResumePicker

        info = await self.push_screen_wait(ResumePicker(self.engine.workdir, language=self._language))
        if info is None:
            await self._greet()  # cancelled → fresh session
        else:
            await self._load_session(info)
        self.query_one(ChatInput).focus()

    async def _load_session(self, info) -> None:
        """Carry a stored session into this one — shared by the picker and the
        direct `--resume <id>` path."""
        from rockycode.session import load_history, load_planning_state, public_id

        try:
            carried = self.engine.resume(load_history(info.path), from_session=info.session_id,
                                         planning_state=load_planning_state(info.path))
        except ValueError as exc:
            await self._add(Static(escape(self._t('app.session_not_loaded', p0=exc)), classes="error-msg"))
            return
        from rockycode.tui.tasks import attach
        attach(self, info)
        self._render_cwd()
        await self._replay(carried)
        await self._add(Static(
            self._t('app.resumed_msgs', p0=public_id(info.session_id), p1=escape(info.display_title), p2=info.n_messages), classes="tool-line"))
        if self.engine.planning.active:
            await self._add(Static(self._t('app.plan_mode_restored'),
                                   classes="tool-line"))
        if self.engine.resumed_mode:
            from rockycode.engine.modes import discover
            fam = next((f for f, ms in discover(self.engine.workdir).items()
                        if self.engine.resumed_mode in ms), "research")
            await self._add(Static(
                self._t('app.mode_was_active', p0=escape(self.engine.resumed_mode), p1=fam, p2=escape(self.engine.resumed_mode)),
                classes="tool-line"))
        label = self._t("app.permission")
        await self._add(Static(
            f"[dim]· {label}: {self._permission_note()}[/]", classes="tool-line"))

    def _maybe_title(self) -> None:
        """Name the session once its first exchange exists: a background flash
        call appends a title record to the trajectory (the exit card and resume
        picker read it). Best-effort — offline/no-key just leaves the
        first-message fallback."""
        if self._titled:
            return
        from rockycode.engine import images as images_mod
        first_user = first_reply = ""
        for m in self.engine.history:
            role, c = m.get("role"), images_mod.content_text(m.get("content"))
            if not c.strip():
                continue
            if role == "user" and not first_user and not c.startswith("[harness]"):
                first_user = c
            elif role == "assistant" and first_user and not first_reply:
                first_reply = c
                break
        if not first_user or not first_reply:
            return  # nothing titleable yet — try again after the next turn
        self._titled = True
        self._title_worker(first_user, first_reply)

    @work(group="title", exclusive=False, exit_on_error=False)
    async def _title_worker(self, first_user: str, first_reply: str) -> None:
        from rockycode.engine.titler import generate_title

        t = await generate_title(self.engine.client, first_user, first_reply, model=self.engine.model)
        if t:
            self.engine.trajectory.title(t)
            if getattr(self, "_task_session", None):
                self._task_session.retitle(t)  # an untitled task's note takes the session's name

    async def _replay(self, messages: list[dict]) -> None:
        """Render a carried conversation so the prior session is visible."""
        from rockycode.engine import images as images_mod
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "user":
                # An image-bearing turn is list content — show its text + a chip.
                text = images_mod.content_text(content)
                if text and not text.startswith("[harness]"):
                    await self._add(Static(f"[bold {VIOLET}]you ▸[/] {escape(text)}", classes="user-msg"))
                n_img = images_mod.count_images(content)
                if n_img:
                    await self._add(Static(
                        f"  [{LAVENDER}]❖ {n_img} image{'s' if n_img > 1 else ''}[/]",
                        classes="tool-line"))
            elif role == "assistant":
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        try:
                            args = json.loads(tc["function"].get("arguments") or "{}")
                        except (TypeError, ValueError):
                            args = {}
                        await self._add(Static(self._tool_text(f"[{PURPLE}]⚒[/]", tc["function"]["name"], args),
                                               classes="tool-line"))
                if isinstance(content, str) and content.strip():
                    await self._add(Static(f"[bold {PURPLE}]♪♫ rocky ▸[/]", classes="rocky-label"))
                    md = Markdown(
                        enhance_markdown(content, width=self._reply_width(),
                                         only_existing=True),
                        parser_factory=rocky_markdown_parser,
                        open_links=False,
                    )
                    await self._add(md)

    @work(group="mcp", exclusive=False)
    async def _connect_mcp(self) -> None:
        mgr = self.engine.mcp_manager
        runtime = getattr(self.engine, "runtime", None)
        if runtime is not None:
            await runtime.start_mcp()
        else:
            await mgr.start()
            self.engine.registry.update(mgr.tools())
        ok = len(mgr.actors)
        n_tools = len(mgr.tools())
        line = self._t('app.mcp_ready_server_s_tools', p0=ok, p1=n_tools)
        if mgr.failures:
            line += self._t('app.failed_mcp_for_details', p1=len(mgr.failures))
        await self._add(Static(line, classes="tool-line"))

    @work(group="lsp", exclusive=False)
    async def _connect_lsp(self) -> None:
        mgr = self.engine.lsp_manager
        try:
            runtime = getattr(self.engine, "runtime", None)
            from rockycode.engine.lsp import build_lsp_tools
            if runtime is not None:
                await runtime.start_lsp()
                if runtime.lsp_error:
                    raise RuntimeError(runtime.lsp_error)
            else:
                await mgr.start()
                await asyncio.wait_for(mgr.ready, timeout=30)
                await mgr.start_cleanup_loop()
                self.engine.registry.update(build_lsp_tools(mgr))
            lsp_tools = build_lsp_tools(mgr)
            n_tools = len(lsp_tools)
            await self._add(
                Static(self._t('app.lsp_ready', p0=mgr.command.split('/')[-1], p1=n_tools),
                       classes="tool-line")
            )
        except Exception as exc:
            await self._add(
                Static(self._t('app.lsp_unavailable', p0=exc),
                       classes="tool-line")
            )

    async def on_unmount(self) -> None:
        registry = getattr(self.engine, "artifact_registry", None)
        if registry is not None:
            registry.unsubscribe(self._artifact_changed)
        await self._delivery.close()
        if getattr(self, "_task_session", None):
            self._task_session.close()
        runtime = getattr(self.engine, "runtime", None)
        if runtime is not None:
            runtime.sandbox = self.sandbox  # includes /sandbox toggles
            await runtime.close()
            return
        mgr = getattr(self.engine, "mcp_manager", None)
        if mgr is not None:
            try:
                await asyncio.wait_for(mgr.stop(), timeout=8)
            except asyncio.TimeoutError:
                pass
        lsp_mgr = getattr(self.engine, "lsp_manager", None)
        if lsp_mgr is not None:
            try:
                await lsp_mgr.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.sandbox is not None and self.sandbox.is_running:
            try:
                await self.sandbox.stop()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        if self._artifact_server is not None:
            try:
                await self._artifact_server.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---- loops (/loop — engine/cron.py) --------------------------------------

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _at_bottom(self) -> bool:
        """Is the transcript pinned to the bottom? If the user scrolled up to
        read, we leave their position alone instead of yanking them back."""
        try:
            t = self._transcript()
        except NoMatches:
            return True  # a screen (e.g. goal) is on top of chat — nothing to pin
        return t.scroll_offset.y >= t.max_scroll_y - 2

    async def _add(self, widget) -> None:
        if self._left:
            return  # rocky was told to leave: a late line is dropped, it must not fail the exit
        try:
            transcript = self._transcript()
        except NoMatches:
            # The app is tearing down (a late loop tick or worker after exit) or
            # a screen is on top of chat: nothing to append to, and a crashed
            # worker must not fail the exit.
            return
        stick = self._at_bottom()
        await transcript.mount(widget)
        if stick:
            transcript.scroll_end(animate=False)

    # NOT named _flush: Textual's App._flush(stderr=...) is a real method it
    # calls internally to flush its output streams. Overriding it shadowed
    # Textual's and crashed intermittently ("unexpected keyword argument
    # 'stderr'") whenever Textual hit that path.
    async def _flush_buffers(self) -> None:
        # A pushed screen (e.g. the goal view) can be on top of chat while this
        # timer keeps ticking — the chat transcript isn't queryable then, so bail.
        try:
            self._transcript()
        except NoMatches:
            return
        dirty = False
        stick = self._at_bottom()
        if self._think_widget is not None and len(self._think_buf) != self._think_flushed:
            self._think_widget.update(self._think_buf)
            self._think_flushed = len(self._think_buf)
            dirty = True
        if self._reply_widget is not None and len(self._reply_buf) != self._reply_flushed:
            self._reply_widget.update(self._reply_buf)  # plain Static — no markdown parse
            self._reply_flushed = len(self._reply_buf)
            dirty = True
        if dirty and stick:
            self._transcript().scroll_end(animate=False)

    def _set_status(self, state: AgentState) -> None:
        self._state = state
        self._render_status()

    def _animate_status(self) -> None:
        # Only the tiny #status widget repaints — the transcript is untouched.
        if self._state in (AgentState.THINKING, AgentState.RESPONDING, AgentState.AMAZED):
            self._note_i += 1
            self._render_status()
        # SSE connects/disconnects independently of an engine event. Refresh the
        # one-line session badge here so browser-tab counts never go stale.
        self._render_cwd()

    def _render_status(self) -> None:
        note = NOTES[self._note_i % len(NOTES)]
        base = _status_text(self._state, note, self._language)
        base += " " + self._t("app.delivery_mode", mode=self._delivery_mode)
        if self._delivery.pending:
            key = "app.queue_paused" if self._delivery.paused else "app.queue_count"
            base += " " + self._t(key, count=len(self._delivery.pending))
        if self.sandbox is not None and self.sandbox.is_running:
            base += f" [{VIOLET}]⬢[/]"
        try:
            self.query_one("#status", Static).update(base)
        except NoMatches:
            pass  # a screen is on top of chat — the status bar isn't visible now

    def refresh_notes(self, width: int | None = None) -> None:
        """Redraw the note strip from the task store (cheap; unchanged = no repaint)."""
        tasks = getattr(self, "_task_session", None)
        try:
            strip = self.query_one(NoteStrip)
        except NoMatches:
            return  # a screen is on top of chat
        strip.show(tasks.notes() if tasks else [], tasks.id if tasks and tasks.registered else None,
                   self._t, width or self.size.width)

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_notes(event.size.width)  # App.size still holds the old width in this handler

    def open_task_note(self, task_id: str | None) -> None:
        """A click on a note: the same path as typing `/task open <id>` (the
        `+N` note lists the older tasks instead)."""
        from rockycode.tui import tasks as tasks_mod
        text = f"/task open {task_id}" if task_id else "/task list"
        self.run_worker(tasks_mod.command(self, text), group="task-note", exclusive=True)

    async def action_quit(self) -> None:
        """ctrl+q — same path as /exit, so the final reflect isn't bypassed."""
        self._request_exit()

    def exit(self, *args, **kwargs) -> None:
        """From this call on there is no screen to draw on: Textual refuses every
        mount. Work that is still unwinding must find that normal (see _add)."""
        self._left = True
        if self._final_reflect is not None:
            self._final_reflect.cancel()  # a second /exit or ctrl+q: no reflect after leaving
        super().exit(*args, **kwargs)

    def _request_exit(self) -> None:
        """Start the exit flow WITHOUT blocking the caller. The final reflect
        awaits an API call, so it must run in a worker — awaiting it inside a
        message handler (slash command / binding) would block the App's message
        pump and deadlock the whole UI (the same reason _after_cancel is a worker)."""
        self.run_worker(self._exit_with_reflect(), exclusive=False)

    async def _handle_slash(self, text: str) -> None:
        await commands.dispatch(self, text)

    @on(ChatInput.Submitted)
    async def _on_submit(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self._exiting:
            # rocky is leaving: nothing new starts. A second /exit still means "leave now".
            if text.split()[0] == "/exit":
                self._request_exit()
            else:
                await self._add(Static(self._t("app.message_unsent", error=self._t("app.leaving"),
                                               text=escape(text)), classes="tool-line"))
            return
        if text.startswith("!"):
            self._run_shell(text[1:].strip())  # worker; don't block the pump
            return
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        images = self._collect_images(text)
        # Cancellation may await tool cleanup; keep it off the UI message pump.
        self.run_worker(self._submit_delivery(text, images, self._delivery_mode, self._artifact_edit_target),
                        group="delivery-input", exclusive=False)

    async def _submit_delivery(self, text, images, mode, artifact_target=None):
        worker = self._turn_worker
        if mode == "queue" and not self._delivery.running and worker and worker.is_running:
            self._delivery.paused = True
            self._waiting_external = True
        elif mode == "interrupt":
            self._waiting_external = False
        try:
            receipt = await self._delivery.submit(text, images=images, mode=mode)
            if artifact_target:
                self._artifact_turn_contexts[receipt["message_id"]] = dict(artifact_target)
                if self._artifact_edit_target == artifact_target:
                    self._cancel_artifact_edit()
            if receipt["status"] == "queued":
                await self._add(Static(self._t("app.message_queued", text=escape(text[:120])), classes="tool-line"))
            return receipt
        except ValueError as exc:
            await self._add(Static(self._t("app.message_unsent", error=escape(str(exc)), text=escape(text)), classes="tool-line"))
            return None

    async def _run_delivery(self, item):
        if self._exiting:
            return False  # rocky is leaving: a message that was still on its way starts no turn
        steps = self._pickups.pop(item.prompt, ())
        self._approver.arm(steps)  # the steps the user allowed may run in THIS turn, the one that asks for them
        try:
            return await self._deliver(item)
        finally:
            self._approver.disarm(steps)  # a pass the turn did not use ends with it

    @staticmethod
    async def _ended(worker) -> str:
        """How a turn worker ended: "done", "cancelled" or "failed". It never
        raises. _deliver waits for it behind a shield, and since Python 3.14 the
        event loop reports a shielded task that ends in an error by itself
        ("WorkerCancelled exception in shielded future") on every stopped turn."""
        try:
            await worker.wait()
        except WorkerCancelled:
            return "cancelled"
        except WorkerFailed:
            return "failed"
        return "done"

    async def _deliver(self, item):
        previous = self._turn_worker
        if previous and previous.is_running:
            if item.mode == "interrupt":
                if self._tick_loop is not None:
                    lp = self._tick_loop
                    await self._add(Static(
                        self._t('commands.loop_interrupted', p1=lp.id, p2=lp.ticks, p3=cron_mod.fmt_duration(lp.interval_s)), classes="tool-line"))
                self._delivery_worker = previous
                previous.cancel()
            waiter = asyncio.create_task(self._ended(previous))
            try:
                ended = await asyncio.shield(waiter)
            except asyncio.CancelledError:
                previous.cancel()
                await waiter
                await self._after_cancel()
                self._delivery_worker = None
                return False
            if ended == "cancelled":
                await self._after_cancel()
            elif ended == "failed":
                self._delivery_worker = None
                return False
        await self._add(Static(self._t('commands.you', p1=escape(item.prompt)), classes="user-msg"))
        if item.images:
            names = " · ".join(Path(p).name for p in item.images)
            await self._add(Static(self._t('commands.image_count', p0=LAVENDER, p1=len(item.images), p2=('s' if len(item.images)>1 else '') if self._language=='en' else '', p3=escape(names)), classes="tool-line"))
        target = self._artifact_turn_contexts.pop(item.id, None)
        text, images = self._artifact_prompt(item.prompt, target), item.images
        try:
            while True:
                self._delivery_failed = False
                worker = self._run_turn(text, images)
                self._turn_worker = self._delivery_worker = worker
                waiter = asyncio.create_task(self._ended(worker))
                try:
                    ended = await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    worker.cancel()
                    await waiter
                    if self._turn_worker is worker:
                        await self._after_cancel()
                    self._plan_go = None
                    return False
                if ended != "done":
                    if self._turn_worker is worker:
                        await self._after_cancel()
                    return False
                if self._delivery_failed:
                    return False
                if not self._plan_go:
                    return True
                text, self._plan_go = self._plan_go, None
                images = None
        finally:
            self._delivery_worker = None

    @work(group="shell", exclusive=True, exit_on_error=False)
    async def _run_shell(self, cmd: str) -> None:
        """`! <cmd>`: run a shell command directly (no model round-trip).
        Output shows here AND lands in rocky's context, so the next message
        can refer to it — same semantics as Claude Code's `!` prefix.

        Runs in a worker (not on the message pump): `! npm install` can take
        minutes, and awaiting it inline froze the whole UI — no typing, no
        ctrl+q — until it finished. Its own group so a new turn won't cancel it."""
        if not cmd:
            await self._add(Static(self._t('commands.shell_usage'), classes="tool-line"))
            return
        if "bash" not in self.engine.registry:
            await self._add(Static(self._t('commands.shell_unavailable'), classes="tool-line"))
            return
        await self._add(Static(f"[bold {AMBER}]$[/] {escape(cmd)}", classes="user-msg"))
        output, _ok = await tools_mod.execute(
            self.engine.registry, "bash", json.dumps({"command": cmd})
        )
        shown = output if len(output) <= 3000 else output[:3000] + self._t('commands.view_truncated')
        await self._add(Static(escape(shown), classes="tool-line"))
        # Into history via the engine so the trajectory logs it too.
        self.engine._append(
            {"role": "user", "content": f"[user ran a shell command]\n$ {cmd}\n{output}"}
        )

    @work(group="turn", exclusive=True)
    async def _run_turn(self, text: str, images: list[str] | None = None) -> None:
        """Drive one turn; turn the modal's Esc (CancelTurn) into a clean stop.
        A worker .cancel() (new submit / Esc binding) is handled in
        on_worker_state_changed — run_turn's finally already keeps history valid."""
        from rockycode.tui.permission import CancelTurn
        try:
            if images:
                # dragged/typed files skip the paste worker's downscale — cap
                # them here (cached by content hash; scaled pastes pass through)
                from rockycode.engine import images as images_mod
                images = [str(await asyncio.to_thread(images_mod.downscale_for_api, p))
                          for p in images]
            if images and not self.engine.vision_enabled:
                images = await self._route_images(text, images)
            await self._drive_turn(text, images)
            await self._after_parking()
            await self._maybe_plan_gate()
            self._maybe_title()  # first completed turn names the session (async, free)
        except CancelTurn:
            self._delivery_failed = True
            await self._flush_buffers()
            await self._finalize_reply()
            self._set_status(AgentState.IDLE)
            await self._add(Static(self._t('app.turn_cancelled'), classes="tool-line"))
            self.query_one(ChatInput).focus()
        except Exception as e:  # noqa: BLE001
            self._delivery_failed = True
            # Never let one turn crash the whole app. The @work default
            # exit_on_error tears down the TUI on any uncaught exception — a
            # mid-stream network drop (loop iterates the stream outside its own
            # try), a MountError from /clear mid-reply, an unexpected API error.
            # Surface it as a line and recover instead. (CancelledError is a
            # BaseException, so a worker .cancel() still propagates to
            # on_worker_state_changed untouched by this handler.)
            try:
                await self._flush_buffers()
                await self._finalize_reply()
            except Exception:  # noqa: BLE001 — best-effort; don't mask the original
                pass
            self._set_status(AgentState.IDLE)
            await self._add(Static(f"✗ {escape(f'{type(e).__name__}: {e}')}", classes="error-msg"))
            self.query_one(ChatInput).focus()

    def _error_text(self, ev: EngineError) -> str:
        """A failed request in rocky's words: what happened and the next step,
        then the provider's own text, dimmed, for whoever needs it."""
        key = f"errors.{ev.kind}"
        worded = key
        if ev.kind:
            facts = {name: escape(str(value)) for name, value in ev.params.items()}
            try:
                worded = self._t(key, **facts)
            except (KeyError, IndexError, ValueError):
                worded = key
        if worded == key:  # not a model request failure, or a kind with no sentence: the engine's own
            return f"✗ {escape(ev.message)}"
        return "✗ " + worded + (f"\n  [dim]{escape(ev.detail[:300])}[/]" if ev.detail else "")

    @staticmethod
    def _tool_text(mark: str, tool: str, args: dict) -> str:
        """`  ✓ bash python -m pytest -q` — the mark, the tool, what it acts on."""
        brief = tool_brief(tool, args)
        return f"  {mark} {tool}" + (f" [{LAVENDER}]{escape(brief)}[/]" if brief else "")

    async def _drive_turn(self, text: str, images: list[str] | None = None) -> None:
        self._think_buf = ""
        self._reply_buf = ""
        self._think_flushed = 0
        self._reply_flushed = 0
        self._think_widget = None
        self._think_box = None
        self._reply_widget = None
        # The engine's first state arrives with the model's first token; until
        # then the screen used to sit still, however long the network took.
        self._set_status(AgentState.THINKING)

        async for ev in self.engine.run_turn(text, images=images):
            if isinstance(ev, StateChanged):
                self._set_status(ev.state)
                if ev.state == AgentState.RESPONDING and self._think_box is not None:
                    self._think_box.collapsed = True

            elif isinstance(ev, ThinkingDelta):
                if self._think_widget is None:
                    # markup=False: model thinking is raw text; a stray "[/…]"
                    # in it would otherwise crash the Static render (as the reply
                    # stream below already guards against).
                    self._think_widget = Static(markup=False, classes="thinking-body")
                    self._think_box = Collapsible(
                        self._think_widget, title=self._t('app.rocky_thinking'), collapsed=False
                    )
                    await self._add(self._think_box)
                self._think_buf += ev.text

            elif isinstance(ev, TextDelta):
                if self._reply_widget is None:
                    await self._add(Static(f"[bold {PURPLE}]♪♫ rocky ▸[/]", classes="rocky-label"))
                    # Stream into a cheap plain Static; markdown is parsed ONCE
                    # at finalize, not on every 100ms flush (the CPU win).
                    self._reply_widget = Static(markup=False, classes="reply-stream")
                    await self._add(self._reply_widget)
                self._reply_buf += ev.text

            elif isinstance(ev, ToolStarted):
                # One line per call, naming what it acts on: the command, the file,
                # the query. The same line becomes the result when the call ends.
                line = Static(self._tool_text(f"[{PURPLE}]⚒[/]", ev.tool, ev.args) + " [dim]…[/]", classes="tool-line")
                self._tool_lines[ev.call_id] = (line, ev.args)
                await self._add(line)

            elif isinstance(ev, ToolFinished):
                mark = f"[{PURPLE}]✓[/]" if ev.ok else f"[{RED}]✗[/]"
                first = ev.output.strip().splitlines()[0] if ev.output.strip() else ""
                line, args = self._tool_lines.pop(ev.call_id, (None, {}))
                text = (self._tool_text(mark, ev.tool, args)
                        + f" [dim]({ev.duration_s:.1f}s)[/] [dim]{escape(first[:80])}[/]")
                if not ev.ok and first.startswith("[parked]"):
                    # Not a failure: yolo kept the step for the user. The sentence after "[parked]" is for the model.
                    text = self._tool_text(f"[{AMBER}]⏸[/]", ev.tool, args) + " " + self._t('parked.tool_line')
                if line is not None and line.is_attached:
                    line.update(text)
                else:  # no start was seen for this call: still say how it ended
                    await self._add(Static(text, classes="tool-line"))
                # Fresh thinking + reply blocks after each tool round, so a
                # post-tool summary lands BELOW the tool output as its own
                # bubble instead of merging into an earlier one.
                await self._reset_step_blocks()

            elif isinstance(ev, Compacted):
                await self._add(
                    Static(
                        self._t('app.context_squeezed_tokens', p0=ev.tokens_before, p1=ev.tokens_after, p2=ev.strategy),
                        classes="tool-line",
                    )
                )

            elif isinstance(ev, CacheReset):
                # Upstream eviction, caught from the API's own hit numbers —
                # rocky-caused resets (switch/compaction/mode) stay silent.
                # One muted line: informative for long sessions, never a nag.
                idle_m = ev.idle_s / 60
                await self._add(
                    Static(
                        self._t('app.cache_evicted', p1=idle_m, p2=ev.hit_tokens, p3=ev.expected_tokens),
                        classes="tool-line",
                    )
                )

            elif isinstance(ev, ContextReminder):
                await self._add(
                    Static(
                        self._t('app.context_reminder', p0=ev.pct, p1=fmt_tokens(ev.window)),
                        classes="tool-line",
                    )
                )

            elif isinstance(ev, EngineError):
                if not ev.message.startswith("compaction summary failed"):
                    self._delivery_failed = True
                await self._add(Static(self._error_text(ev), classes="error-msg"))

            elif isinstance(ev, TurnFinished):
                u = ev.usage
                if u:
                    await self._add(
                        Static(
                            self._t('app.step_s_in_out_cache_hit', p0=ev.steps, p1=u.get('prompt_tokens', 0), p2=u.get('completion_tokens', 0), p3=u.get('prompt_cache_hit_tokens', 0)),
                            classes="usage-line",
                        )
                    )
                    self._ledger.add(self.engine.model, u)
                    self._update_total()

        if self._approver.stop_turn and self._tick_loop is None:
            # The engine ended the turn at the park limit: queued messages wait for the user, as after Esc.
            self._delivery_failed = True
        await self._flush_buffers()
        await self._finalize_reply()

    async def _finalize_reply(self) -> None:
        """Replace the plain streaming Static with a one-shot Markdown render
        when the reply block is done — markdown is parsed once, not per flush."""
        if self._reply_widget is None:
            return
        # Claimed before the first await: a turn stopped in the middle of the swap
        # below is finalized again by its clean-up, and must find nothing left to do.
        # (It used to mount the rendered reply after a line that was already going.)
        widget, text = self._reply_widget, self._reply_buf
        self._reply_widget, self._reply_buf, self._reply_flushed = None, "", 0
        try:
            transcript = None if self._left else self._transcript()
        except NoMatches:
            transcript = None  # the app is closing under the turn: nothing to draw on
        if text.strip() and transcript is not None:
            # width=0 before first layout → None → links yes, wrapping no.
            md = Markdown(
                enhance_markdown(text, width=self._reply_width(),
                                 only_existing=True),
                parser_factory=rocky_markdown_parser,
                open_links=False,
            )
            await transcript.mount(md, after=widget)
            await gone(widget)

    def _update_total(self) -> None:
        # Price directly in the chosen currency from its own official table —
        # no usd→cny conversion (see pricing.py). Peak surcharge is applied per
        # request inside cost().
        amt = self._ledger.cost(self._currency)
        sym = "¥" if self._currency == "cny" else "$"
        t = self._ledger.totals()
        if self._ledger.all_free():
            # every turn ran on this machine: $0 by design, and the cache-rate
            # story is an API-provider thing — say `local` instead.
            self.query_one("#total", Static).update(
                self._t('app.0_in_out_local', p1=t['prompt'], p2=t['completion']))
            return
        note = (self._t('app.search_fees_incomplete') if not self._ledger.search_usage_complete else
                "" if self._ledger.configured(self._currency) else self._t('app.prices_unset'))
        rate = (t["hit"] / t["prompt"] * 100) if t["prompt"] else 0.0
        self.query_one("#total", Static).update(
            self._t('app.in_out_cache', p1=sym, p2=amt, p3=t['prompt'], p4=t['completion'], p5=rate, p6=note)
        )

    async def _reset_step_blocks(self) -> None:
        """After a tool round, the next model step gets fresh thinking + reply
        blocks, so each think/act/answer round reads as its own block — and a
        post-tool summary is its own bubble below the tool output, not glued
        to a pre-tool preamble above it."""
        if self._think_widget is not None:
            self._think_widget.update(self._think_buf)
        self._think_buf = ""
        self._think_flushed = 0
        self._think_widget = None
        self._think_box = None
        await self._finalize_reply()


def run_app(
    engine: Engine, *, resume: bool = False, resume_session=None, sandbox=None,
    currency: str = "usd", theme: str = "dark", language: str = "auto", ui_language: str = "auto",
    permission: str = "ask", permission_weakened: bool = False,
    evolve: str = "auto",
):
    """Run the chat TUI. /goal runs in its own screen inside the app and pops back,
    so this just runs the app to normal exit."""
    return RockyCodeApp(
        engine, resume=resume, resume_session=resume_session, sandbox=sandbox,
        currency=currency, theme=theme, language=language, ui_language=ui_language,
        permission=permission, permission_weakened=permission_weakened,
        prompt_history=PromptHistory(),  # the real CLI persists; tests stay in-memory
        evolve=evolve,
    ).run()
