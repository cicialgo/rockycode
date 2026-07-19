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
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea
from textual.worker import Worker, WorkerState

from rockycode.banner import ROCKY_TAGLINE
from rockycode.engine import AgentState, Engine
from rockycode.engine import tools as tools_mod
from rockycode.engine.effort import EFFORT_LEVELS, to_deepseek
from rockycode.engine.events import (
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
from rockycode.tui.mdterm import (
    enhance_markdown,
    link_click_action,
    rocky_markdown_parser,
)
from rockycode.tui.mdview import TEXT_DOC_SUFFIXES, WIDTHS, DocDock
from rockycode.tui.prompt_history import PromptHistory

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

# Rocky speaks in musical chords — the note cycles while he thinks/sings.
NOTES = "♪♫♬♩"


def _status_text(state: AgentState, note: str) -> str:
    return {
        AgentState.IDLE: "",
        AgentState.THINKING: f"[italic {VIOLET}]{note} thinking…[/]",
        AgentState.RESPONDING: f"[italic {LAVENDER}]{note} singing…[/]",
        AgentState.TOOL: f"[italic {MUTED}]⚒ working…[/]",
        AgentState.COMPACTING: f"[italic {MUTED}]♻ squeeze context…[/]",
        AgentState.AMAZED: f"[bold {VIOLET}]✦ amaze! {note}[/]",
        AgentState.ERROR: f"[{RED}]✗ no good[/]",
    }[state]

HELP_TEXT = f"""\
[bold {VIOLET}]commands[/]
  [{LAVENDER}]/help[/]    show this
  [{LAVENDER}]/plan [topic|off][/]  plan first — read-only explore + brainstorm into a plan file you approve
  [{LAVENDER}]/goal [objective][/]  go autonomous — plan → confirm → work, in its own view (needs Docker)
  [{LAVENDER}]/research[/]  research mode — pick how we work: deep-research · paper-reading · whiteboard
  [{LAVENDER}]/learn[/]   learn mode — rocky tutors you through a paper, a codebase, or a concept
  [{LAVENDER}]/model[/]   switch provider + model (deepseek · minimax · glm · kimi · mimo)
  [{LAVENDER}]/sandbox[/]  sandbox on | off | status — isolate tools in a container
  [{LAVENDER}]/lsp[/]     language-server status (diagnostics ride along read_file)
  [{LAVENDER}]/artifact[/]  artifact live on | off — auto-refresh artifacts in the browser
  [{LAVENDER}]/prompt[/]  show rocky's system prompt
  [{LAVENDER}]/mcp[/]     show connected MCP servers + tools
  [{LAVENDER}]/skills[/]  show installed skills
  [{LAVENDER}]/memory[/]  show what rocky remembers
  [{LAVENDER}]/proposals[/]  review skills rocky drafted while dreaming (approve or archive)
  [{LAVENDER}]/routines[/]  run or lease due routines (recurring, sandboxed, budgeted)
  [{LAVENDER}]/remember <note>[/]  save a note to memory (feedback)
  [{LAVENDER}]/config [key] [value][/]  show or set preferences — language auto|en|zh · theme · currency (restart to apply)
  [{LAVENDER}]/effort [off|high|xhigh|max][/]  how hard i think — applies from the next reply
  [{LAVENDER}]/permission [yolo|ask|careful][/]  show or switch approval mode (this session)
  [{LAVENDER}]! <cmd>[/]  run a shell command; output goes into rocky's context
  [{LAVENDER}]/clear[/]   clear the transcript
  [{LAVENDER}]/exit[/]    quit (also /quit or ctrl+q)"""

WELCOME = (
    f"[bold {PURPLE}]♪♫ rocky ▸[/]\n"
    "hello hello! i rocky. you ask, we fix bug together. amaze!\n"
    f"[{MUTED}]try: “read the README and explain this repo”[/]"
)


class TextViewerScreen(ModalScreen):
    """Scrollable full-screen viewer for long text (system prompt, files).

    A modal owns keyboard focus, so arrows/PageUp/PageDown scroll naturally —
    in the transcript those keys belong to the Input.
    """

    BINDINGS = [("escape", "close", "close"), ("q", "close", "close")]

    DEFAULT_CSS = """
    TextViewerScreen { align: center middle; }
    TextViewerScreen > VerticalScroll {
        width: 90%;
        height: 90%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        body = VerticalScroll(Static(self._text))
        body.border_title = self._title
        body.border_subtitle = "↑/↓/pgup/pgdn scroll · esc close"
        yield body

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss()


class ArtifactLiveModal(ModalScreen):
    """Ask once: open artifacts live (local server, auto-refresh) or static?

    dismiss() → "live" | "static".  Esc → "static" (the safe default).
    ↑↓/←→ move between the two choices, Enter picks the focused one.
    """

    BINDINGS = [
        ("escape", "static", "static"),
        # Buttons only move on Tab by default — let the arrows walk between them
        # too (they sit side by side, so either axis feels natural). Enter on the
        # focused button picks it (Button's own binding).
        ("left", "prev", ""),
        ("up", "prev", ""),
        ("right", "next", ""),
        ("down", "next", ""),
    ]

    CSS = """
    ArtifactLiveModal { align: center middle; }
    ArtifactLiveModal > Vertical {
        width: 66; height: auto; padding: 1 2;
        background: $surface; border: round $primary;
    }
    ArtifactLiveModal .q { margin-bottom: 1; }
    ArtifactLiveModal Horizontal { height: auto; align: center middle; }
    ArtifactLiveModal Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold {VIOLET}]rocky want show artifact![/]\n"
                "open it [bold]live[/]? rocky start small local server so the "
                "browser tab auto-refresh when rocky rebuild it. amaze!\n"
                f"[{MUTED}](else just open once as a file — /artifact live on later)[/]",
                classes="q",
            )
            with Horizontal():
                yield Button("open live", variant="primary", id="live")
                yield Button("just once", id="static")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_next(self) -> None:
        self.focus_next()

    def action_prev(self) -> None:
        self.focus_previous()

    def action_static(self) -> None:
        self.dismiss("static")


class ChatInput(TextArea):
    """Multi-line chat input. Enter sends; Ctrl+J inserts a newline; paste keeps
    its newlines. Grows with content up to MAX_LINES, then scrolls — so a
    pasted error or a few lines of code are actually visible while you type.

    (Shift+Enter would be the usual newline key, but the kitty keyboard
    protocol is disabled for IME support, so it's indistinguishable from
    Enter — Ctrl+J is the reliable alternative.)
    """

    MAX_LINES = 8

    SLASH_COMMANDS = [
        "/help", "/plan", "/goal", "/research", "/learn", "/sandbox", "/lsp", "/artifact",
        "/prompt", "/config", "/model", "/effort", "/permission", "/mcp", "/skills", "/memory",
        "/proposals", "/routines", "/remember", "/clear", "/exit", "/quit",
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, *, history: PromptHistory | None = None, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, **kwargs)
        # None → in-memory only (every direct construction in tests stays
        # hermetic); the CLI injects a store so history survives restarts.
        self._store = history
        self._history: list[str] = list(history.items) if history else []
        self._hist_idx: int | None = None
        self._draft = ""                # the in-progress text before browsing history
        self._suggest_widget: Static | None = None

    def on_mount(self) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        # Count VISUAL rows (soft-wrapped), not just logical newlines, so a
        # single long line or a wrapped CJK/pasted block grows the box to fit
        # instead of staying collapsed to one row. wrapped_document.height
        # reflects the current wrap width; fall back to logical lines before the
        # widget has been laid out (width still 0).
        try:
            lines = self.wrapped_document.height or self.document.line_count
        except Exception:  # noqa: BLE001 — height math must never break typing
            lines = self.document.line_count if self.document else 1
        # +2 for the rounded border (border-box height includes it)
        self.styles.height = min(max(lines, 1), self.MAX_LINES) + 2

    def on_resize(self, _event: events.Resize) -> None:
        # Terminal width changed → text re-wraps → recompute visual rows so the
        # box still fits. TextArea._on_resize still runs its own re-wrap.
        self._sync_height()

    def _recall(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)

    def _cursor_visual_row(self) -> tuple[int, int]:
        """(row, total) of the cursor among VISUAL rows — soft-wrap aware, so a
        long line that wraps to several rows is counted by what's on screen, not
        by newlines. cursor_location[0] is the *logical* line and would treat a
        wrapped single line as one row (up/down would recall history mid-text)."""
        try:
            wd = self.wrapped_document
            return wd.location_to_offset(self.cursor_location).y, wd.height
        except Exception:  # noqa: BLE001 — cursor math must never break typing
            return self.cursor_location[0], self.document.line_count

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                if not self._history or self._history[-1] != text:
                    self._history.append(text)
                if self._store is not None:
                    self._store.append(text)
                self._hist_idx = None
                self._draft = ""
                self.post_message(self.Submitted(text))
                self.text = ""
                self._sync_height()
            return
        if event.key == "ctrl+j":  # reliable manual newline
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        # Up/Down recall previous inputs — but only from the top/bottom VISUAL
        # row, so while there's text above/below the cursor they just move a row
        # within it (like an editor). Only past the top row does up reach into
        # history — and the in-progress text is saved as the draft first.
        vrow, vtotal = self._cursor_visual_row()
        if event.key == "up" and vrow == 0 and self._history:
            event.prevent_default()
            event.stop()
            if self._hist_idx is None:
                self._draft = self.text
                self._hist_idx = len(self._history) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            self._recall(self._history[self._hist_idx])
            return
        if event.key == "down" and self._hist_idx is not None \
                and vrow == vtotal - 1:
            event.prevent_default()
            event.stop()
            if self._hist_idx < len(self._history) - 1:
                self._hist_idx += 1
                self._recall(self._history[self._hist_idx])
            else:
                self._hist_idx = None
                self._recall(self._draft)
            return
        if event.key == "tab":
            text = self.text.strip()
            if text.startswith("/"):
                prefix = text.lower()
                matches = [c for c in self.SLASH_COMMANDS if c.lower().startswith(prefix)]
                if len(matches) == 1:
                    event.prevent_default()
                    event.stop()
                    self.text = matches[0] + " "
                    self.move_cursor(self.document.end)
                    self._hide_suggestions()
                    return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def _on_changed(self, _event: TextArea.Changed) -> None:
        self._sync_height()
        text = self.text.strip()
        if text.startswith("/") and len(text) > 1 and "\n" not in text:
            prefix = text.lower()
            matches = [c for c in self.SLASH_COMMANDS if c.lower().startswith(prefix)]
            if matches:
                self._show_suggestions(matches, prefix)
            else:
                self._show_suggestions([f"  ? no match — /help for all commands"], "")
        else:
            self._hide_suggestions()

    def _show_suggestions(self, matches: list[str], prefix: str) -> None:
        try:
            hint = self.app.query_one("#hints", Static)
        except Exception:  # noqa: BLE001
            return
        line = "  ".join(f"[bold]{m}[/]" if m.startswith(prefix) else m for m in matches[:8])
        if self._suggest_widget is None:
            hint.update(line)
        self._suggest_widget = hint

    def _hide_suggestions(self) -> None:
        if self._suggest_widget is not None:
            try:
                self.app.query_one("#hints", Static).update(
                    "/help · /model · /effort · /sandbox · /prompt · /config · /mcp · /skills · /memory · !cmd · /clear · /exit"
                )
            except Exception:  # noqa: BLE001
                pass
            self._suggest_widget = None

    # Some terminals (iTerm2) route the wheel to the focused input instead of
    # the widget under the pointer, so the transcript never scrolls. Forward
    # wheel events from the input to the transcript so it scrolls regardless.
    def _scroll_chat(self, dy: int) -> None:
        try:
            self.app.query_one("#transcript").scroll_relative(y=dy, animate=False)
        except Exception:  # noqa: BLE001 — best effort; never break input on scroll
            pass

    def on_mouse_scroll_up(self, event) -> None:
        self._scroll_chat(-3)
        event.stop()

    def on_mouse_scroll_down(self, event) -> None:
        self._scroll_chat(3)
        event.stop()


class RockyCodeApp(App):
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
    #modechip { width: auto; margin-right: 2; }
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
    ]

    def __init__(
        self, engine: Engine, *, resume: bool = False, resume_session=None, sandbox=None,
        currency: str = "usd", theme: str = "dark",
        permission: str = "ask", permission_weakened: bool = False,
        prompt_history: PromptHistory | None = None,
        exit_sheet: str = "auto", dream: str = "auto",
    ) -> None:
        super().__init__()
        self.engine = engine
        self._prompt_history = prompt_history
        self._resume_requested = resume
        self._resume_session = resume_session  # SessionInfo → skip the picker
        self._titled = False  # one title record per session file (see _maybe_title)
        self._currency = currency
        self._theme_pref = theme
        self._permission_mode = permission
        self._permission_weakened = permission_weakened
        self._auto_approve: set[str] = set()    # non-bash tools OK'd this session
        self._auto_approve_bins: set[str] = set()  # bash BINARIES OK'd this session
        self._turn_worker: Worker | None = None  # captured so Esc can cancel it
        from rockycode.pricing import UsageLedger
        self._ledger = getattr(engine, "ledger", None) or UsageLedger()
        self.sandbox = sandbox  # ChatSandbox | None — created on /sandbox on
        self._local_registry: dict | None = None  # saved so we can swap back
        self._sandbox_starting = False  # guard concurrent /sandbox on
        self._lsp_connecting = False  # guard concurrent LSP start
        # Artifact live mode: None = undecided (ask on first artifact), True/False set.
        self._artifact_live = getattr(engine, "artifact_live", None)
        self._artifact_server = None  # ArtifactServer once live mode starts
        # Set by the plan gate's "approve": the implement-turn text, submitted
        # from on_worker_state_changed once the current plan-mode turn finishes.
        self._plan_go: str | None = None
        self._exit_sheet_open = False  # re-entry guard for /exit + ctrl+q
        self._exit_sheet_fut: asyncio.Future | None = None  # so a 2nd /exit can skip
        self._exit_sheet = exit_sheet  # "auto" | "on" | "off" (config; live via /config)
        self._dream_mode = dream       # "auto" | "manual" — launch catch-up dream
        # Did the launch probe find Ollama? None = not probed (dream off/manual,
        # memory off, or exited before the worker ran). "auto" sheets key off
        # this: no live dream consumer → the user is never asked for feedback.
        self._ollama_ok: bool | None = None

    # animate=False on purpose: instant, snappy scroll (the default animation
    # lags on a fast keypress and, in headless pilot runs, hadn't applied yet).
    def action_transcript_up(self) -> None:
        self._transcript().scroll_page_up(animate=False)

    def action_transcript_down(self) -> None:
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
        w = self._turn_worker
        if w is not None and w.is_running:
            w.cancel()
            return
        await self.action_doc_close()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
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
        elif (event.worker is self._turn_worker
              and event.state is WorkerState.SUCCESS and self._plan_go):
            text, self._plan_go = self._plan_go, None
            self._turn_worker = self._run_turn(text)

    async def _after_cancel(self) -> None:
        await self._flush_buffers()
        await self._finalize_reply()
        await self._add(Static(f"[{MUTED}]· turn cancelled[/]", classes="tool-line"))
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort during teardown
            pass

    async def _approve_tool(self, name: str, args: dict) -> bool:
        """Engine approver (wired in on_mount for ask/careful). Runs the pure
        policy; only asks when it says 'ask' and the tool isn't already allowed
        for the session. The prompt is shown INLINE (see _ask_inline) so the
        transcript stays scrollable while it waits. Esc aborts the whole turn."""
        from rockycode.engine.permission import decide, session_grantable, sniff_danger
        from rockycode.tui.permission import CancelTurn

        tool = self.engine.registry.get(name)
        risk = getattr(tool, "risk", "risky")
        verdict = decide(self._permission_mode, risk, args, self.engine.workdir,
                         tool=name, read_grants=self.engine.read_grants)
        if verdict == "block":
            # A never-allowed command (e.g. sudo rm -rf /). Refuse in every mode —
            # this is the system blocking, not the user denying, so say so loudly.
            await self._add(Static(
                f"[bold {RED}]⛔ blocked a dangerous command[/] "
                f"[dim]{escape(self._tool_detail(name, args))}[/]", classes="error-msg"))
            return False
        if verdict == "allow":
            return True  # in-jail (or already-granted) → nothing new to grant
        # verdict == "ask": a session grant only covers non-dangerous calls, and
        # for bash it's scoped to the BINARY, not "all bash" — approving `lake
        # build` lets `lake …` run but a later `curl`/`rm` still prompts.
        from rockycode.engine.permission import command_binary

        if session_grantable(name, args):
            if name == "bash":
                binary = command_binary(str(args.get("command", "")))
                if binary and binary in self._auto_approve_bins:
                    return True
            elif name in self._auto_approve:
                self._grant_read_if_escaping(name, args)
                return True
        # A dangerous bash command may NEVER be session-granted → no "allow"
        # option is offered (session_label=None); only run-once or deny.
        session_label = None
        if session_grantable(name, args):
            if name == "bash":
                binary = command_binary(str(args.get("command", "")))
                if binary:
                    session_label = f"Allow `{binary}` for this session"
            else:
                session_label = f"Allow {name} for this session"
        choice = await self._ask_inline(
            name, self._tool_detail(name, args), risk, sniff_danger(name, args),
            session_label=session_label,
        )
        if choice in ("once", "session"):
            if choice == "session":
                if name == "bash":
                    b = command_binary(str(args.get("command", "")))
                    if b:
                        self._auto_approve_bins.add(b)
                else:
                    self._auto_approve.add(name)
            self._grant_read_if_escaping(name, args)  # make the approval REAL: widen the read jail
            return True
        if choice == "cancel":
            raise CancelTurn()
        return False  # "deny" (or dismissed) → model gets a [denied] result

    def _grant_read_if_escaping(self, name: str, args: dict) -> None:
        """Approving an out-of-workdir read must actually let it happen — the hard
        jail (tools._jail) blocks it otherwise, so the approval would be a lie.
        Record the resolved path in engine.read_grants (reads only); the jail +
        permission layer both consult it, so it now runs and won't re-prompt.
        Secret files (_is_secret_file) are still refused inside a granted path."""
        if name != "read_file":
            return
        p = args.get("path")
        if not isinstance(p, str) or not p:
            return
        try:
            target = Path(p)
            if not target.is_absolute():
                target = self.engine.workdir / target
            target = target.resolve()
            wd = self.engine.workdir.resolve()
        except (OSError, ValueError, RuntimeError):
            return
        roots = (wd, *self.engine.allowed_roots, *self.engine.read_grants)
        if not any(target == r or r in target.parents for r in roots):
            self.engine.read_grants.add(target)  # escapes every root → grant it

    # ---- plan mode ----------------------------------------------------------

    async def _handle_plan(self, text: str) -> None:
        """/plan [topic] — enter plan mode; /plan off — leave it.

        Plan mode makes the ENGINE read-only (engine/planmode.py gates every tool
        call before the approver — even in yolo) except for one plan file the
        model drafts. The instruction rides each user turn, so toggling never
        touches the system prompt or tool schemas (the prefix cache stays warm)."""
        from rockycode.engine import planmode

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() == "off":
            if self.engine.plan_file is None:
                await self._add(Static(f"[{MUTED}]plan mode is not on — /plan [topic] to start[/]",
                                       classes="tool-line"))
                return
            kept = self.engine.plan_file.relative_to(self.engine.workdir)
            self.engine.plan_file = None
            self._render_cwd()
            await self._add(Static(
                f"[{VIOLET}]✦ plan mode off[/] [dim]— {kept} is kept for reference; "
                "rocky acts on it only if you ask[/]", classes="tool-line"))
            return
        if self.engine.plan_file is not None:
            await self._add(Static(
                f"[{MUTED}]already planning → {self.engine.plan_file.relative_to(self.engine.workdir)} · "
                f"/plan off to leave[/]", classes="tool-line"))
            return
        path = planmode.create_plan_file(self.engine.workdir, topic=arg)
        self.engine.plan_file = path
        self._render_cwd()
        await self._add(Static(
            f"[{VIOLET}]✦ plan mode[/] — read-only: rocky explores + brainstorms first, then drafts the plan\n"
            f"  [dim]plan file:[/] {path.relative_to(self.engine.workdir)}\n"
            f"  [dim]code stays untouched · /plan off to leave[/]", classes="tool-line"))

    def _plan_digest(self) -> "str | None":
        """Content hash of the plan file (None when plan mode is off / unreadable).
        Compared before/after a turn: a change IS the handoff signal."""
        pf = self.engine.plan_file
        if pf is None:
            return None
        try:
            return hashlib.sha1(pf.read_bytes()).hexdigest()
        except OSError:
            return None

    async def _maybe_plan_gate(self, before: "str | None") -> None:
        """Show the plan-approval gate (inline) when this plan-mode turn changed
        the plan file. Fires at turn END only, so a mid-turn draft never
        interrupts. approve → mode off + implement turn (submitted once this
        worker finishes); discuss → keep planning, input prefilled; exit → mode
        off, file kept; Esc → keep planning silently."""
        from rockycode.tui.plangate import InlinePlanGate

        pf = self.engine.plan_file
        if pf is None:
            return
        after = self._plan_digest()
        if after is None or after == before:
            return
        try:
            plan_text = pf.read_text(errors="replace").strip()
        except OSError:
            return
        if not plan_text:
            return
        rel = pf.relative_to(self.engine.workdir)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        widget = InlinePlanGate(str(rel), plan_text, fut)
        await self.mount(widget, before=self.query_one("#prompt"))
        try:
            choice = await fut
        finally:
            if widget.is_mounted:
                await widget.remove()
            try:
                self.query_one(ChatInput).focus()
            except Exception:  # noqa: BLE001
                pass
        if choice == "approve":
            self.engine.plan_file = None
            self._render_cwd()
            await self._add(Static(
                f"[{VIOLET}]✦ plan approved[/] [dim]— plan mode off, building now[/]", classes="tool-line"))
            self._plan_go = (f"Plan approved — implement the plan in {rel} now, phase by phase. "
                             "Stay inside its scope.")
        elif choice == "goal":
            # Hand the approved plan to goal: it executes the SAME plan file in the
            # sandbox (skipping its own planner), then pops back to chat.
            await self._plan_to_goal(pf, rel)
        elif choice == "discuss":
            inp = self.query_one(ChatInput)
            inp.text = "revise the plan: "
            inp.focus()
        elif choice == "exit":
            self.engine.plan_file = None
            self._render_cwd()
            await self._add(Static(
                f"[{VIOLET}]✦ plan mode off[/] [dim]— {rel} is kept for reference; "
                "rocky acts on it only if you ask[/]", classes="tool-line"))
        # "dismiss" (Esc) → keep planning silently

    async def _plan_to_goal(self, plan_file, rel) -> None:
        """Hand an approved plan file to goal: it runs the SAME plan in the sandbox
        (skipping its own planner), then pops back to chat. Docker-gated."""
        import shutil

        self.engine.plan_file = None   # leaving plan mode; goal takes over
        self._render_cwd()
        if shutil.which("docker") is None or not await self._docker_ready():
            await self._add(Static(
                f"[{AMBER}]◆ goal needs Docker running[/] [dim]— start it, then run /goal; "
                f"or approve to build {rel} here instead. Your plan {rel} is kept.[/]",
                classes="tool-line"))
            return
        from rockycode.tui.goal_screen import GoalScreen

        backend = self._make_goal_backend(f"implement the approved plan ({rel})", plan_file=plan_file)
        await self._add(Static(
            f"[bold {VIOLET}]◆ goal[/] [dim]— running your approved plan in the sandbox "
            f"(autonomous, isolated); back here when it's done.[/]", classes="tool-line"))
        summary = await self.push_screen_wait(GoalScreen(backend, f"plan: {rel}"))
        await self._on_goal_return(summary)

    async def _ask_inline(
        self, tool: str, detail: str, risk: str, warning: str | None,
        session_label: str | None = None,
    ) -> str:
        """Mount an InlineApproval just above the input and await the user's
        choice. Docking it into the chat (rather than a modal that owns the
        screen) keeps the transcript visible and scrollable — pageup / shift+↑↓
        / the wheel all still work while the prompt waits. Returns one of
        'once' | 'session' | 'deny' | 'cancel'."""
        from rockycode.tui.permission import InlineApproval

        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        widget = InlineApproval(tool, detail, risk, warning, fut, session_label=session_label)
        await self.mount(widget, before=self.query_one("#prompt"))
        try:
            return await fut
        finally:
            if widget.is_mounted:
                await widget.remove()
            # give focus back to the input so the user can keep typing / scrolling
            try:
                self.query_one(ChatInput).focus()
            except Exception:  # noqa: BLE001 — app may be tearing down
                pass

    def _tool_detail(self, name: str, args: dict) -> str:
        """A human-readable one-liner of what the call will do, for the modal."""
        if name == "bash":
            return args.get("command", "") or "(no command)"
        if name == "web_fetch":
            return args.get("url", "") or "(no url)"
        if name in ("write_file", "edit_file"):
            return f"{name} → {args.get('path', '?')}"
        if name == "remember":
            return f"remember: {str(args.get('name') or args.get('content', ''))[:140]}"
        try:
            return json.dumps(args)[:400]
        except (TypeError, ValueError):
            return str(args)[:400]

    def _permission_note(self) -> str:
        return {
            "yolo": "yolo — no prompts (esc still stops a turn)",
            "careful": "careful — every write & command asks first (esc stops a turn)",
        }.get(self._permission_mode, "ask — risky actions ask first (esc stops a turn)")

    def _perm_chip(self) -> str:
        """The always-visible status-bar permission indicator (red for yolo)."""
        if self._permission_mode == "yolo":
            return f"[{RED}]🔓 yolo · no prompts[/]"
        if self._permission_mode == "careful":
            return f"[{MUTED}]🔒 careful[/]"
        return f"[{MUTED}]🔒 ask[/]"

    def _effort_note(self) -> str:
        """Current effort-dial reading, wire-honest: names what DeepSeek is sent
        when the dial value and the provider tier differ (xhigh → max)."""
        if not self.engine.thinking:
            return (f"[{LAVENDER}]off[/] [dim](thinking disabled — "
                    f"{self.engine.reasoning_effort} kept for when it's back on)[/]")
        eff = self.engine.reasoning_effort
        sent = to_deepseek(eff)
        wire = f" [dim](sends {sent} to deepseek)[/]" if sent != eff else ""
        return f"[{LAVENDER}]{eff}[/]{wire}"

    def _render_cwd(self) -> None:
        plan = f"   [{VIOLET}]📋 plan[/]" if self.engine.plan_file is not None else ""
        self.query_one("#cwd", Static).update(
            f"[{MUTED}]📁 {self.engine.workdir.name}[/]{plan}   {self._perm_chip()}"
        )

    def _set_permission_mode(self, mode: str) -> None:
        """Flip the approval mode for this session (does NOT persist — use /config
        for that). Tightening clears the session allowlist so tools previously
        OK'd via 'allow all' will ask again; loosening leaves it untouched."""
        rank = {"yolo": 0, "ask": 1, "careful": 2}
        if rank[mode] > rank[self._permission_mode]:
            self._auto_approve.clear()
            self._auto_approve_bins.clear()  # tightening revokes bash binary grants too
        self._permission_mode = mode
        self._render_cwd()

    def _in_sandbox(self) -> bool:
        return self.sandbox is not None and self.sandbox.is_running

    async def _maybe_nudge_yolo_host(self) -> None:
        """When yolo is on but tools run on the real machine (no sandbox), say
        plainly what that means and point at /sandbox to contain it. Only fires in
        the exposed state, so it's quiet once a sandbox is on or the mode tightens."""
        if self._permission_mode != "yolo" or self._in_sandbox():
            return
        await self._add(Static(
            f"[bold {AMBER}]⚠ yolo runs on your machine[/] [dim]— no prompts, so installs, network "
            f"and git push happen for real. (rocky still refuses the truly destructive.)[/]\n"
            f"  [dim]want it contained? → [/][{LAVENDER}]/sandbox on[/][dim] runs tools in a throwaway "
            f"box: offline, your files untouched.[/]",
            classes="tool-line"))

    # Wheel anywhere scrolls the transcript. Textual scrolls a widget natively
    # when the pointer is over it; these app-level handlers catch wheel events
    # that bubble up (e.g. over the input or a gap), so the transcript scrolls
    # regardless of where the pointer is — works the same in iTerm2, Ghostty,
    # and the VS Code terminal (all send mouse events; Textual enables them).
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
        what = f"{n} lines" if n > 1 else (f"{len(text)} chars" if len(text) > 24 else repr(text))
        self.notify(f"⧉ copied {what}", timeout=2)

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

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(
                f"[bold {VIOLET}]ROCKYCODE[/] [{MUTED}]· {self.engine.model}[/]\n"
                f"[italic {MUTED}]{ROCKY_TAGLINE}[/]",
                id="title-block",
            )
            yield Static("", id="status")
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
        prompt.border_title = "ask rocky"
        prompt.border_subtitle = "enter ↵ send · ctrl+j newline · shift+↑↓ / pgup·pgdn scroll history"
        yield prompt
        with Horizontal(id="statusbar"):
            yield Static(f"[{MUTED}]📁 {self.engine.workdir.name}[/]", id="cwd")
            yield Static("", id="modechip")
            yield Static("", id="total")
        yield Static(f"[{MUTED}]drag text = copy · /help · /research · /learn · /model · /effort · /sandbox · /artifact · /permission · /config · /mcp · /skills · /memory · /proposals · !cmd · /clear · /exit[/]", id="hints")

    async def on_mount(self) -> None:
        self.register_theme(ROCKY_THEME)
        self.register_theme(ROCKY_THEME_LIGHT)
        self.theme = "rocky-light" if self._theme_pref == "light" else "rocky"
        # Wire the interactive approver — always, even in yolo, so /permission can
        # flip modes mid-session; decide() short-circuits yolo to "allow" (no modal)
        # at negligible cost. Headless/bench never set an approver, so they keep the
        # engine's zero-overhead always-allow default.
        self.engine.approver = self._approve_tool
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
        self._transcript().border_title = "♪ chat"
        # Persistent permission reminder in the bottom bar — always visible, so a
        # no-prompt (yolo) session can't be forgotten mid-research.
        self._render_cwd()
        self._render_mode_chip()  # a launch-time config mode lights the chip
        if self._permission_weakened:
            await self._add(Static(
                f"[bold {RED}]⚠ this folder's .rockycode/config.toml lowered permission to "
                f"'{self._permission_mode}' — rocky runs with fewer prompts here than your "
                f"default. (see the chip in the status bar)[/]", classes="error-msg"))
        # Inside the TUI, not the pre-launch console — the alt screen covers
        # anything printed before the app starts (found in cici's live test:
        # the warning was only visible after exit, i.e. exactly too late).
        for w in project_env_warnings(self.engine.workdir):
            await self._add(Static(f"[{AMBER}]· {escape(w)}[/]", classes="tool-line"))
        if os.environ.get("TERM_PROGRAM") == "iTerm.app":
            await self._add(Static(
                "[dim]· iterm2: clicks, links and drag-copy need Settings → Profiles → "
                "Terminal → 'Enable mouse reporting' (⌥-drag keeps native selection)[/]",
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
        # Dream proposals waiting from earlier passes — one muted, clickable-by-
        # command line; the inbox never nags beyond this.
        try:
            from rockycode.dream.proposals import ProposalStore
            from rockycode.session import get_project
            n = len(ProposalStore().list(project_id=get_project(self.engine.workdir).id))
            if n:
                await self._add(Static(
                    f"[{MUTED}]🌙 {n} dream proposal(s) waiting — /proposals to review[/]",
                    classes="tool-line"))
        except Exception:  # noqa: BLE001 — the inbox must never break startup
            pass
        # Due routines: leased ones run themselves (that's what the lease
        # bought); the rest get one muted line. Catch-up, never a daemon.
        try:
            from rockycode.routines import RoutineStore
            from rockycode.session import get_project
            rstore = RoutineStore()
            due = rstore.due(project_id=get_project(self.engine.workdir).id)
            leased = [r for r in due if rstore.lease_active(r)]
            manual = [r for r in due if not rstore.lease_active(r)]
            if manual:
                names = ", ".join(r.name for r in manual)
                await self._add(Static(
                    f"[{MUTED}]⏰ routine(s) due: {names} — /routines to run[/]",
                    classes="tool-line"))
            for r in leased:
                self._run_routine_bg(r)
        except Exception:  # noqa: BLE001 — routines must never break startup
            pass

    async def _greet(self) -> None:
        await self._add(Static(WELCOME, classes="rocky-label"))
        await self._add(Static(f"[dim]· permission: {self._permission_note()}[/]", classes="tool-line"))
        for fname in getattr(self.engine, "project_notes", []) or []:
            await self._add(Static(f"[dim]· loaded {fname} (project instructions)[/]", classes="tool-line"))
        n_skills = len(getattr(self.engine, "skills", []) or [])
        if n_skills:
            await self._add(
                Static(f"[dim]· {n_skills} skill(s) available — /skills to list[/]", classes="tool-line")
            )
        if getattr(self.engine, "web_enabled", False):
            order = " → ".join(getattr(self.engine, "web_order", ()) or ())
            await self._add(Static(f"[dim]· web tools on (search: {order})[/]", classes="tool-line"))
        if getattr(self.engine, "mcp_manager", None) and self.engine.mcp_manager.configs:
            names = ", ".join(self.engine.mcp_manager.configs)
            await self._add(Static(f"[dim]· connecting mcp servers: {names}…[/]", classes="tool-line"))
            self._connect_mcp()
        if getattr(self.engine, "lsp_manager", None) is not None:
            await self._add(Static(f"[dim]· connecting lsp…[/]", classes="tool-line"))
            self._connect_lsp()
        # Artifacts are lazy: create_artifact calls _artifact_target, which asks
        # once (unless --live) and starts the server on demand. No idle server.
        self.engine.artifact_target = self._artifact_target

    @work
    async def _resume_flow(self) -> None:
        from rockycode.tui.resume import ResumePicker

        info = await self.push_screen_wait(ResumePicker(self.engine.workdir))
        if info is None:
            await self._greet()  # cancelled → fresh session
        else:
            await self._load_session(info)
        self.query_one(ChatInput).focus()

    async def _load_session(self, info) -> None:
        """Carry a stored session into this one — shared by the picker and the
        direct `--resume <id>` path."""
        from rockycode.session import load_history, public_id

        carried = self.engine.resume(load_history(info.path), from_session=info.session_id)
        await self._replay(carried)
        await self._add(Static(
            f"[dim]· resumed {public_id(info.session_id)} · {escape(info.display_title)} · "
            f"{info.n_messages} msgs[/]", classes="tool-line"))
        if self.engine.resumed_mode:
            from rockycode.engine.modes import discover
            fam = next((f for f, ms in discover(self.engine.workdir).items()
                        if self.engine.resumed_mode in ms), "research")
            await self._add(Static(
                f"[dim]· mode {escape(self.engine.resumed_mode)} was active in that session — "
                f"/{fam} {escape(self.engine.resumed_mode)} to re-enter[/]",
                classes="tool-line"))
        await self._add(Static(
            f"[dim]· permission: {self._permission_note()}[/]", classes="tool-line"))

    def _render_mode_chip(self) -> None:
        name = self.engine.mode_name
        self.query_one("#modechip", Static).update(
            f"[{LAVENDER}]◉ {name}[/]" if name else "")

    async def _handle_mode_cmd(self, family: str, text: str) -> None:
        """/research and /learn — bare opens the picker (or applies the single
        typeless mode), `<name>` is the direct path, `off` leaves, `always`
        makes it this folder's launch default, `never` clears that default."""
        from rockycode.engine.modes import discover, resolve

        args = text.split()[1:]
        arg = args[0].lower() if args else ""
        if arg == "off":
            if self.engine.mode_name is None:
                await self._add(Static(f"[{MUTED}]· no mode active[/]", classes="tool-line"))
            else:
                self.engine.clear_mode()
                self._render_mode_chip()
                await self._add(Static(
                    f"[{MUTED}]· mode off — plain rocky again[/]", classes="tool-line"))
            return
        if arg == "always":
            name = args[1].lower() if len(args) > 1 else (self.engine.mode_name or "")
            if not name:
                await self._add(Static(
                    f"[{MUTED}]· nothing to persist — enter a mode first (/{family}), "
                    f"then /{family} always[/]", classes="tool-line"))
                return
            # Built-ins only: a folder default is applied silently at launch, so a
            # cloned repo's local mode file must never ride in through it.
            mode, err = resolve(family, name, workdir=self.engine.workdir, builtin_only=True)
            if mode is None:
                await self._add(Static(
                    f"[{MUTED}]· {escape(err)} (only built-in modes can be a folder default)[/]",
                    classes="tool-line"))
                return
            from rockycode.config import set_project_value
            _v, werr = set_project_value(self.engine.workdir, "mode", mode.name)
            if werr:
                await self._add(Static(f"[{AMBER}]? {escape(werr)}[/]", classes="tool-line"))
                return
            if self.engine.mode_name != mode.name:
                await self._apply_mode(family, mode)
            await self._add(Static(
                f"[{MUTED}]· saved — rocky starts in {mode.name} mode in this folder "
                f"(undo: /{family} never)[/]", classes="tool-line"))
            return
        if arg == "never":
            from rockycode.config import set_project_value
            _v, werr = set_project_value(self.engine.workdir, "mode", "")
            await self._add(Static(
                f"[{AMBER}]? {escape(werr)}[/]" if werr else
                f"[{MUTED}]· folder default cleared — modes are per-session again[/]",
                classes="tool-line"))
            return
        if arg:
            mode, err = resolve(family, arg, workdir=self.engine.workdir)
            if mode is None:
                await self._add(Static(f"[{MUTED}]· {escape(err)}[/]", classes="tool-line"))
                return
            await self._apply_mode(family, mode)
            return
        # Bare command: a single-mode family (typeless /learn) applies directly;
        # otherwise — or when a mode is active, so "off" stays reachable — picker.
        modes = list(discover(self.engine.workdir).get(family, {}).values())
        if not modes:
            await self._add(Static(f"[{MUTED}]· no {family} modes installed[/]", classes="tool-line"))
        elif len(modes) == 1 and self.engine.mode_name is None:
            await self._apply_mode(family, modes[0])
        else:
            self._mode_picker_flow(family, modes)

    @work
    async def _mode_picker_flow(self, family: str, modes: list) -> None:
        from rockycode.tui.modepicker import ModePicker

        result = await self.push_screen_wait(
            ModePicker(family, modes, active=self.engine.mode_name))
        if result == "off":
            self.engine.clear_mode()
            self._render_mode_chip()
            await self._add(Static(
                f"[{MUTED}]· mode off — plain rocky again[/]", classes="tool-line"))
        elif result is not None:
            await self._apply_mode(family, result)
        self.query_one(ChatInput).focus()

    async def _apply_mode(self, family: str, mode) -> None:
        """Swap the contract in + the entry card that teaches the direct path
        (same teach-at-use pattern as the resume exit card)."""
        self.engine.set_mode(mode.name, mode.body)
        self._render_mode_chip()
        await self._add(Static(
            f"[bold {LAVENDER}]◉ {mode.name}[/] [{MUTED}]— {escape(mode.description)}[/]",
            classes="tool-line"))
        direct = f"/{family}" if mode.name == family else f"/{family} {mode.name}"
        await self._add(Static(
            f"[{MUTED}]  leave with /{family} off · folder default: /{family} always · "
            f"next time, jump straight in: {direct}[/]", classes="tool-line"))

    def _maybe_title(self) -> None:
        """Name the session once its first exchange exists: a background flash
        call appends a title record to the trajectory (the exit card and resume
        picker read it). Best-effort — offline/no-key just leaves the
        first-message fallback."""
        if self._titled:
            return
        first_user = first_reply = ""
        for m in self.engine.history:
            role, c = m.get("role"), m.get("content")
            if not isinstance(c, str) or not c.strip():
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

        t = await generate_title(self.engine.client, first_user, first_reply)
        if t:
            self.engine.trajectory.title(t)

    async def _replay(self, messages: list[dict]) -> None:
        """Render a carried conversation so the prior session is visible."""
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "user":
                if isinstance(content, str) and not content.startswith("[harness]"):
                    await self._add(Static(f"[bold {VIOLET}]you ▸[/] {escape(content)}", classes="user-msg"))
            elif role == "assistant":
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        await self._add(Static(f"  [{PURPLE}]⚒[/] {tc['function']['name']}", classes="tool-line"))
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
        await mgr.start()
        self.engine.registry.update(mgr.tools())
        ok = len(mgr.actors)
        n_tools = len(mgr.tools())
        line = f"[dim]· mcp ready: {ok} server(s), {n_tools} tools[/]"
        if mgr.failures:
            line += f" [{MUTED}]({len(mgr.failures)} failed — /mcp for details)[/]"
        await self._add(Static(line, classes="tool-line"))

    async def _artifact_target(self):
        """Decide live vs static for artifacts, lazy-starting the server.

        Called by create_artifact. Asks once (unless --live already decided),
        remembers the answer for the session, and returns the running
        ArtifactServer (live) or None (static file://).
        """
        if self._artifact_live is False:
            return None
        if self._artifact_live is None:
            choice = await self.push_screen_wait(ArtifactLiveModal())
            self._artifact_live = choice == "live"
            if not self._artifact_live:
                await self._add(Static(
                    "[dim]· artifacts: static (file://) — /artifact live on to enable live updates[/]",
                    classes="tool-line"))
                return None
        if self._artifact_server is None:
            from rockycode.engine.artifact import ArtifactServer
            srv = ArtifactServer(self.engine.workdir)
            await srv.start()
            self._artifact_server = srv
            self.engine.artifact_server = srv
            await self._add(Static(
                f"[dim]· artifacts live at {srv.base_url} — tab auto-refreshes on rebuild[/]",
                classes="tool-line"))
        return self._artifact_server

    @work(group="lsp", exclusive=False)
    async def _connect_lsp(self) -> None:
        mgr = self.engine.lsp_manager
        try:
            await mgr.start()  # default session
            await asyncio.wait_for(mgr.ready, timeout=30)
            await mgr.start_cleanup_loop()
            # Register the 4 active LSP tools now that the server is ready.
            from rockycode.engine.lsp import build_lsp_tools
            lsp_tools = build_lsp_tools(mgr)
            self.engine.registry.update(lsp_tools)
            n_tools = len(lsp_tools)
            await self._add(
                Static(f"[dim]· lsp ready ({mgr.command.split('/')[-1]}) — "
                       f"diagnostics + {n_tools} tools[/]",
                       classes="tool-line")
            )
        except Exception as exc:
            await self._add(
                Static(f"[dim]· lsp unavailable: {exc} — diagnostics disabled[/]",
                       classes="tool-line")
            )

    @work(group="dream", exclusive=True, exit_on_error=False)
    async def _dream_catchup(self) -> None:
        """Phase 1 slice: the catch-up dream. At launch, quietly consolidate
        this project's UN-DREAMED past sessions on local Ollama (free, private
        — the only reader allowed to see exit-sheet feedback). Silent unless
        it actually dreamed: no memory store / no Ollama / lock held / nothing
        pending → no noise. The current session is excluded; it gets its turn
        on a future launch, after its outcome record exists."""
        import httpx

        from rockycode.dream.core import OLLAMA_URL, DreamRunner

        if self._dream_mode != "auto" or getattr(self.engine, "memory_store", None) is None:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as http:
                (await http.get(f"{OLLAMA_URL}/api/tags")).raise_for_status()
        except Exception:  # noqa: BLE001 — no ollama, no dream, no nagging
            self._ollama_ok = False
            return
        self._ollama_ok = True
        index = None
        try:
            from rockycode.memory.index import MemoryIndex
            index = MemoryIndex(self.engine.memory_store)
            index.conn()
        except Exception:  # noqa: BLE001 — semantic index is optional
            index = None
        # The judge reuses the LIVE engine's client + model — no new auth, and
        # the sheet never reaches it (condense's default hides feedback).
        from rockycode.dream.judge import TranscriptJudge
        runner = DreamRunner(
            self.engine.workdir,
            exclude={self.engine.trajectory.session_id},
            judge=TranscriptJudge(self.engine.client, self.engine.model),
        )
        try:
            report = await runner.run(limit=5, index=index)
        except RuntimeError:
            return  # another dream holds the lock — it's being handled
        except Exception:  # noqa: BLE001 — a failed dream must never disturb chat
            return
        if report.sessions_digested:
            drafted = " · drafted a proposal, /proposals" if report.proposals_drafted else ""
            await self._add(Static(
                f"[{MUTED}]🌙 rocky dreamed over {report.sessions_digested} past session(s) — "
                f"+{report.facts_added} facts, {report.facts_updated} updated · "
                f"/memory to browse{drafted}[/]",
                classes="tool-line"))

    @work(group="proposals", exclusive=True, exit_on_error=False)
    async def _review_proposals(self) -> None:
        """Walk this project's pending dream proposals, one inline card each.
        Approve installs the skill globally; archive keeps the file; later
        stops the walk and leaves the rest pending. The card is the trust
        boundary — dream drafts, only a human installs."""
        from rockycode.dream.proposals import ProposalStore
        from rockycode.session import get_project
        from rockycode.tui.proposalcard import ProposalCard

        store = ProposalStore()
        pending = store.list(project_id=get_project(self.engine.workdir).id)
        if not pending:
            await self._add(Static(
                f"[{MUTED}]no pending proposals — rocky drafts them while dreaming, "
                f"when a pattern keeps coming back[/]", classes="tool-line"))
            return
        for p in pending:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            card = ProposalCard(p, fut)
            await self.mount(card, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                if card.is_mounted:
                    await card.remove()
            if choice == "install":
                installed = store.approve(p)
                await self._add(Static(
                    f"[{VIOLET}]✦ skill '{p.name}' installed[/] "
                    f"[dim]→ {installed.parent} · loads next session (/skills)[/]",
                    classes="tool-line"))
            elif choice == "archive":
                store.archive(p)
                await self._add(Static(
                    f"[{MUTED}]· archived '{p.name}' — kept under proposals/archived[/]",
                    classes="tool-line"))
            else:  # later — stop walking, leave the rest pending
                await self._add(Static(
                    f"[{MUTED}]· later — /proposals when you're ready[/]", classes="tool-line"))
                break
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

    @work(group="routines", exclusive=False, exit_on_error=False)
    async def _run_routine_bg(self, r) -> None:
        """Run one routine detached (sandboxed exec) and report a single line.
        Docker down / any failure = a loud line, never a broken chat."""
        from rockycode.routines import RoutineStore, run_routine

        store = RoutineStore()
        await self._add(Static(
            f"[{MUTED}]⏰ routine {r.name} running (sandboxed)…[/]", classes="tool-line"))
        try:
            res = await run_routine(store, r, model=self.engine.model,
                                    client=self.engine.client)
        except Exception as e:  # noqa: BLE001
            await self._add(Static(
                f"[{AMBER}]⏰ routine {r.name} failed: {e}[/]", classes="tool-line"))
            return
        where = r.output_dir or "last-run.md (beside the routine)"
        await self._add(Static(
            f"[{MUTED}]🌙 routine {r.name}: {res['status']} · ${res['cost']:.4f} · "
            f"see {where} · {res['session']}[/]", classes="tool-line"))

    @work(group="routines", exclusive=True, exit_on_error=False)
    async def _review_routines(self) -> None:
        """Walk due routines, one inline card each: run / lease / later / off."""
        from rockycode.routines import RoutineStore
        from rockycode.session import get_project
        from rockycode.tui.routinecard import RoutineCard

        store = RoutineStore()
        due = store.due(project_id=get_project(self.engine.workdir).id)
        if not due:
            await self._add(Static(
                f"[{MUTED}]no routines due — they come back on their cadence[/]",
                classes="tool-line"))
            return
        for r in due:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            card = RoutineCard(r, store.state(r).lease_spent, fut)
            await self.mount(card, before=self.query_one("#prompt"))
            try:
                choice = await fut
            finally:
                if card.is_mounted:
                    await card.remove()
            if choice == "run":
                self._run_routine_bg(r)
            elif choice == "auto":
                store.grant_lease(r, days=7, budget=r.budget_lease)
                await self._add(Static(
                    f"[{VIOLET}]✦ auto lease granted[/] [dim]— {r.name} runs itself for 7 days "
                    f"or ${r.budget_lease:.2f}, whichever ends first; renew from this card[/]",
                    classes="tool-line"))
                self._run_routine_bg(r)
            elif choice == "off":
                r.enabled = False
                store.save(r)
                await self._add(Static(
                    f"[{MUTED}]· {r.name} turned off — re-enable in its routine.toml[/]",
                    classes="tool-line"))
            else:  # later — stop walking
                await self._add(Static(
                    f"[{MUTED}]· later — /routines when you're ready[/]", classes="tool-line"))
                break
        try:
            self.query_one(ChatInput).focus()
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

    async def on_unmount(self) -> None:
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

    # ---- transcript helpers -------------------------------------------------

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
        stick = self._at_bottom()
        await self._transcript().mount(widget)
        if stick:
            self._transcript().scroll_end(animate=False)

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

    async def _handle_model(self, text: str) -> None:
        """/model — switch provider AND exact model live. Bare shows only the
        options you've KEYED (so an EN/CN catalog stays short), with an "N more"
        footer; `/model all` shows the full catalog. A spec (provider,
        provider-region, provider:model, or a unique model id) rebuilds the
        client with that endpoint's base_url + rocky-owned key."""
        from rockycode.engine import providers as P

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg or arg == "all":
            show_all = arg == "all"
            picks = P.choices() if show_all else P.configured_choices()
            lines = [f"[bold {VIOLET}]model[/] [dim](provider + model, this session)[/]",
                     f"  now: [{VIOLET}]{escape(self.engine.provider_name)}:{escape(self.engine.model)}[/]"]
            for c in picks:
                cur = (c.prov_id == self.engine.provider_name and c.model == self.engine.model)
                tag = "" if c.configured else " [dim]✗ no key[/]"
                lines.append(f"  [{'bold ' + VIOLET if cur else LAVENDER}]/model {c.id}[/]"
                             f"[dim] — {escape(c.provider.label)}[/]{tag}")
            if not show_all:
                hidden = len(P.choices()) - len(picks)
                if hidden > 0:
                    lines.append(f"[dim]  + {hidden} more (no key yet) — /model all to see · "
                                 f"keys go in ~/.rockycode as ROCKYCODE_<PROVIDER>[_CN]_API_KEY[/]")
            await self._add(Static("\n".join(lines), classes="tool-line"))
            return

        resolved = P.resolve(arg)
        if resolved is None:
            await self._add(Static(
                f"[{AMBER}]· unknown or ambiguous — /model to see your options, /model all for the catalog[/]",
                classes="tool-line"))
            return
        prov, ep, model = resolved
        prev_model = self.engine.model
        try:
            from rockycode.onboarding import provider_key
            key = provider_key(ep.key_env)
        except Exception as e:  # noqa: BLE001 — missing key: say exactly what to set
            await self._add(Static(f"[{AMBER}]· {escape(str(e))}[/]", classes="tool-line"))
            return
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url=ep.base_url, max_retries=5, timeout=300.0)
        self.engine.switch_provider(
            client, model, provider_name=ep.eid,
            reasoning_policy=prov.reasoning, tools_enabled=(prov.tools == "native"),
        )
        self._render_status()
        note = "" if prov.tools == "native" else " [dim](tools off — plain responder)[/]"
        await self._add(Static(
            f"[{VIOLET}]✦ model → {escape(ep.eid)}:{escape(model)}[/]{note} "
            f"[dim]applies from the next reply[/]", classes="tool-line"))
        # Cost reminder on a real switch: the new model's API rate, and that the
        # prompt cache resets (first turn on it is all cache-miss → pricier).
        if model != prev_model:
            await self._add(Static(self._model_cost_note(model), classes="tool-line"))

    def _model_cost_note(self, model: str) -> str:
        """One-line API-fee reminder for a just-switched model: its input/output
        rate in the session currency, or a nudge to set it. All pricing is
        per-token API fee (not membership/plan) — the ledger prices each turn by
        the model that produced it."""
        sym = "¥" if self._currency == "cny" else "$"
        if self._ledger.priced(model):
            r = self._ledger.rate(model, self._currency)
            body = (f"[dim]· {escape(model)} · API fee {sym}{r['in_miss']:.4g} in / "
                    f"{sym}{r['out']:.4g} out per 1M tok — cache resets on switch "
                    f"(first turn all-miss)[/]")
        else:
            body = (f"[{AMBER}]· {escape(model)} has no price set — add its API fee to "
                    f"~/.rockycode/pricing.toml or cost shows 0. cache resets on switch.[/]")
        return body

    def _render_status(self) -> None:
        note = NOTES[self._note_i % len(NOTES)]
        base = _status_text(self._state, note)
        if self.sandbox is not None and self.sandbox.is_running:
            base += f" [{VIOLET}]⬢[/]"
        try:
            self.query_one("#status", Static).update(base)
        except NoMatches:
            pass  # a screen is on top of chat — the status bar isn't visible now

    # ---- exit + slash commands -----------------------------------------------

    async def action_quit(self) -> None:
        """ctrl+q — same path as /exit, so the feedback sheet isn't bypassed."""
        self._request_exit()

    def _request_exit(self) -> None:
        """Start the exit flow WITHOUT blocking the caller. The sheet awaits a
        Future, so it must run in a worker — awaiting it inside a message
        handler (slash command / binding) would block the App's message pump
        and deadlock the whole UI (the same reason _after_cancel is a worker)."""
        self.run_worker(self._exit_with_sheet(), exclusive=False)

    async def _exit_with_sheet(self) -> None:
        """Leave the app, offering the one-click exit feedback sheet first —
        only when a real exchange happened. The answer lands as a `feedback`
        trajectory record, which is LOCAL ONLY: never placed in a cloud-bound
        prompt; the local dream reads it later (self-evolve phase 0)."""
        from rockycode.tui.exitsheet import NEVER, ExitSheet

        if self._exit_sheet_open:
            # A second /exit or ctrl+q while the sheet waits = "skip, leave now".
            if self._exit_sheet_fut is not None and not self._exit_sheet_fut.done():
                self._exit_sheet_fut.set_result(None)
            return
        # "auto" (the default) asks ONLY when the dream pipeline is alive this
        # session — the probe succeeded, so the feedback has a real local
        # consumer. Users who never dream are never asked (and never left
        # wondering whether a rating goes to the model provider).
        wanted = self._exit_sheet == "on" or (
            self._exit_sheet == "auto" and self._ollama_ok is True)
        if (not wanted
                or self.engine.stats.turns == 0
                or self.engine.trajectory.path is None):
            self.exit()  # sheet off / no dream consumer / nothing to rate
            return
        self._exit_sheet_open = True
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._exit_sheet_fut = fut
        widget = ExitSheet(fut)
        try:
            await self.mount(widget, before=self.query_one("#prompt"))
        except Exception:  # noqa: BLE001 — a covered screen must never block exit
            self._exit_sheet_open = False
            self.exit()
            return
        try:
            result = await fut
        finally:
            self._exit_sheet_open = False
            if widget.is_mounted:
                await widget.remove()
        if result == NEVER:
            # Persist the off-switch from where the annoyance is; /config
            # exit_sheet on brings it back.
            from rockycode.config import set_value
            set_value("exit_sheet", "off")
            self._exit_sheet = "off"
        elif result is not None:
            self.engine.trajectory.feedback({**result, "local_only": True})
        self.exit()

    async def _handle_slash(self, text: str) -> None:
        cmd = text.split()[0].lower()
        if cmd in ("/exit", "/quit"):
            self._request_exit()
        elif cmd == "/proposals":
            self._review_proposals()
        elif cmd == "/routines":
            self._review_routines()
        elif cmd == "/clear":
            await self._transcript().remove_children()
        elif cmd == "/help":
            await self._add(Static(HELP_TEXT, classes="tool-line"))
        elif cmd in ("/research", "/learn"):
            await self._handle_mode_cmd(cmd[1:], text)
        elif cmd == "/config":
            from rockycode.config import DEFAULTS, GLOBAL_PATH, load, set_value
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:  # /config key value  → set
                v, err = set_value(parts[1], parts[2])
                if err:
                    await self._add(Static(f"[{AMBER}]? {err}[/]", classes="tool-line"))
                else:
                    # Model limits take effect on the live engine at once; other
                    # keys (currency/theme/language) are read at startup.
                    if parts[1] in ("context_window", "max_tokens"):
                        setattr(self.engine, parts[1], v)
                        note = "applied now"
                    elif parts[1] == "exit_sheet":
                        self._exit_sheet = v
                        note = "applied now"
                    elif parts[1] == "dream":
                        self._dream_mode = v
                        note = "applies at next launch"
                    else:
                        note = "restart to apply"
                    await self._add(Static(
                        f"[{VIOLET}]✦ saved: {parts[1]} = {v}[/] [dim]({note})[/]",
                        classes="tool-line"))
            else:  # /config  → show
                cfg = load(self.engine.workdir)
                lines = [f"[bold {VIOLET}]config[/] [dim]{GLOBAL_PATH}[/]"]
                lines += [f"  [{LAVENDER}]{k}[/] = {cfg[k]}" for k in DEFAULTS]
                lines.append("[dim]  /config <key> <value> to set[/]")
                await self._add(Static("\n".join(lines), classes="tool-line"))
        elif cmd == "/prompt":
            await self.push_screen(
                TextViewerScreen(
                    "system prompt — edit in rockycode/prompts/rocky.py",
                    self.engine.history[0]["content"],
                )
            )
        elif cmd == "/skills":
            skills = getattr(self.engine, "skills", []) or []
            if not skills:
                await self._add(
                    Static(
                        "[dim]no skills found (.claude/skills, .rockycode/skills, "
                        "~/.claude/skills, ~/.codex/prompts)[/]",
                        classes="tool-line",
                    )
                )
            else:
                lines = [f"[bold {VIOLET}]skills[/]"]
                lines += [f"  [{LAVENDER}]{s.name}[/] — {s.description} [dim]({s.source})[/]" for s in skills]
                await self._add(Static("\n".join(lines), classes="tool-line"))
        elif cmd == "/memory":
            store = getattr(self.engine, "memory_store", None)
            memories = store.load_all() if store is not None else []
            if store is None:
                await self._add(Static("[dim]memory is off (--no-memory)[/]", classes="tool-line"))
            elif not memories:
                await self._add(
                    Static(
                        "[dim]i remember nothing yet. tell me things with /remember "
                        "— files land in .rockycode/memory/[/]",
                        classes="tool-line",
                    )
                )
            else:
                lines = [f"[bold {VIOLET}]memory[/]"]
                lines += [f"  [{LAVENDER}]{m.name}[/] [dim]({m.type})[/dim] — {m.description}" for m in memories]
                lines.append("[dim]  rockycode memory show <name> · files in .rockycode/memory/[/]")
                await self._add(Static("\n".join(lines), classes="tool-line"))
        elif cmd == "/remember":
            store = getattr(self.engine, "memory_store", None)
            note = text[len("/remember"):].strip()
            if store is None:
                await self._add(Static("[dim]memory is off (--no-memory)[/]", classes="tool-line"))
            elif not note:
                await self._add(Static(f"[{MUTED}]usage: /remember <note>[/]", classes="tool-line"))
            else:
                from rockycode.memory import Memory
                mem = Memory(name="", type="feedback", description=note[:80], body=note, origin="user")
                path = store.save(mem)
                await self._add(
                    Static(
                        f"[{VIOLET}]✦ amaze! i remember:[/] {note}\n[dim]  {path}[/]",
                        classes="tool-line",
                    )
                )
        elif cmd == "/plan":
            await self._handle_plan(text)
        elif cmd == "/goal":
            self._handle_goal(text)  # worker: probes Docker without blocking the pump
        elif cmd == "/sandbox":
            self._handle_sandbox(text)  # worker; don't block the pump
        elif cmd == "/lsp":
            mgr = getattr(self.engine, "lsp_manager", None)
            if mgr is None:
                await self._add(Static("[dim]lsp is off (--no-lsp)[/]", classes="tool-line"))
            elif not mgr.is_running:
                await self._add(Static(f"[dim]lsp: configured ({mgr.command}) but not connected[/]", classes="tool-line"))
            else:
                tools = ", ".join(sorted(mgr.available_tools)) or "(listing…)"
                await self._add(
                    Static(
                        f"[{VIOLET}]lsp[/] [{LAVENDER}]connected[/]\n"
                        f"  [dim]server: {mgr.command}\n"
                        f"  tools: {tools}[/]",
                        classes="tool-line",
                    )
                )
        elif cmd == "/artifact":
            parts = text.split()
            sub = parts[1].lower() if len(parts) > 1 else "status"
            arg = parts[2].lower() if len(parts) > 2 else ""
            if sub == "live" and arg == "on":
                self._artifact_live = True
                await self._artifact_target()  # ensure server (announces if it starts)
            elif sub == "live" and arg == "off":
                self._artifact_live = False
                if self._artifact_server is not None:
                    await self._artifact_server.stop()
                    self._artifact_server = None
                    self.engine.artifact_server = None
            state = ("live" if self._artifact_live else
                     "static (file://)" if self._artifact_live is False else
                     "undecided — rocky asks on first artifact")
            url = f" [dim]{self._artifact_server.base_url}[/]" if self._artifact_server is not None else ""
            await self._add(Static(
                f"[bold {VIOLET}]artifact[/] [{LAVENDER}]{state}[/]{url}\n"
                f"  [dim]/artifact live on | off[/]", classes="tool-line"))
        elif cmd == "/mcp":
            mgr = getattr(self.engine, "mcp_manager", None)
            if mgr is None:
                await self._add(Static("[dim]mcp is off (--no-mcp)[/]", classes="tool-line"))
            else:
                lines = [f"[bold {VIOLET}]mcp[/]"]
                lines += [f"  {line}" for line in mgr.status()]
                tools = sorted(mgr.tools())
                if tools:
                    lines.append(f"  [dim]{' · '.join(tools)}[/]")
                await self._add(Static("\n".join(lines), classes="tool-line"))
        elif cmd == "/effort":
            parts = text.split()
            arg = parts[1].lower() if len(parts) > 1 else ""
            if arg in EFFORT_LEVELS:
                if arg == "off":
                    self.engine.thinking = False
                else:
                    self.engine.thinking = True
                    self.engine.reasoning_effort = arg
                await self._add(Static(
                    f"[{VIOLET}]✦ effort → {self._effort_note()}[/] "
                    f"[dim]applies from the next reply (this session)[/]",
                    classes="tool-line"))
            else:
                await self._add(Static(
                    f"[bold {VIOLET}]effort[/] [dim](how hard i think — this session)[/]\n"
                    f"  now: {self._effort_note()}\n"
                    f"  [dim]/effort off | high | xhigh | max — off skips thinking; "
                    f"xhigh = max on deepseek, its own tier elsewhere[/]",
                    classes="tool-line"))
        elif cmd == "/model":
            await self._handle_model(text)
        elif cmd == "/permission":
            parts = text.split()
            if len(parts) >= 2 and parts[1].lower() in ("yolo", "ask", "careful"):
                self._set_permission_mode(parts[1].lower())
                await self._add(Static(
                    f"[{VIOLET}]✦ permission → {self._permission_note()}[/]",
                    classes="tool-line"))
                await self._maybe_nudge_yolo_host()  # switching TO yolo on the host
            else:
                await self._add(Static(
                    f"[bold {VIOLET}]permission[/] [dim](this session — /config persists)[/]\n"
                    f"  now: {self._perm_chip()}\n"
                    f"  [dim]/permission yolo|ask|careful[/]",
                    classes="tool-line"))
        else:
            await self._add(
                Static(
                    f"[{MUTED}]?[/] i no know [bold]{cmd}[/]. try [{LAVENDER}]/help[/]",
                    classes="tool-line",
                )
            )

    def _gather_host_tools(self) -> dict:
        """Return tools that should stay on the host (not enter the sandbox)."""
        host_names = {"web_search", "web_research", "web_fetch", "create_artifact",
                      "lsp_lookup", "lsp_symbol_search", "lsp_file_symbols", "lsp_diagnostics"}
        if self._local_registry is None:
            return {}
        return {k: v for k, v in self._local_registry.items() if k in host_names}

    def _restore_host_tools(self) -> None:
        """Re-add host-side tools after a sandbox→local swap (lost in the swap)."""
        host_tools = self._gather_host_tools()
        if host_tools:
            self.engine.registry.update(host_tools)

    @work(group="goal", exclusive=True, exit_on_error=False)
    async def _handle_goal(self, text: str) -> None:
        """/goal [objective] — go autonomous. With an objective AND Docker ready it
        opens the goal SCREEN on top of chat (plan → confirm → work → summary) and
        pops back here when done. Without an objective (or Docker) it just guides.
        Worker: the docker probe must not block the message pump."""
        import shutil

        obj = text[len("/goal"):].strip()
        if shutil.which("docker") is None:
            docker_up = False
            docker_line = (f"  [{AMBER}]✗ Docker not found[/] — goal needs it for the sandbox. "
                           f"install Docker Desktop, start it, then /goal again.")
        else:
            docker_up = await self._docker_ready()
            docker_line = (f"  [{VIOLET}]✓ Docker ready[/]" if docker_up else
                           f"  [{AMBER}]✗ Docker installed but not running[/] — start Docker Desktop, then /goal again.")

        # Run the goal IN A SCREEN on top of chat (plan → confirm → milestones →
        # summary all rendered here), then pop back — no dropping to a bare terminal.
        if obj and docker_up:
            from rockycode.tui.goal_screen import GoalScreen

            backend = self._make_goal_backend(obj)
            await self._add(Static(
                f"[bold {VIOLET}]◆ goal[/] [dim]— opening the goal view (plan → confirm → work). "
                f"you'll come back here when it's done.[/]\n  [dim]{escape(obj)}[/]",
                classes="tool-line"))
            summary = await self.push_screen_wait(GoalScreen(backend, obj))
            await self._on_goal_return(summary)
            return

        # Otherwise: guide (no objective, or Docker not ready).
        lines = [
            f"[bold {VIOLET}]goal mode[/] — hand rocky an objective and it works "
            f"unattended: on an isolated COPY of your repo, in a Docker sandbox, "
            f"under a budget, committing each milestone. You wake up to a branch to review.",
            docker_line,
        ]
        if not obj:
            lines.append("  [dim]give it an objective:[/] [italic]/goal fix the failing auth test[/]")
        else:
            lines.append("  [dim]once Docker's running, run[/] "
                         f"[{LAVENDER}]/goal {escape(obj)}[/] [dim]again — it opens the goal view here.[/]")
        await self._add(Static("\n".join(lines), classes="tool-line"))

    async def _docker_ready(self) -> bool:
        """True if the Docker daemon answers. Separate so /goal wiring is testable
        without a real daemon."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return (await asyncio.wait_for(proc.wait(), timeout=8)) == 0
        except Exception:  # noqa: BLE001
            return False

    def _make_goal_backend(self, objective: str, *, plan_file=None):
        """Build the live goal backend from the session's model + config. Separate
        so a test can swap in a fake backend and drive the whole /goal loop.
        plan_file (from a /plan handoff) makes goal skip its planner and execute
        the already-approved plan."""
        from rockycode.config import load as load_config
        from rockycode.engine.budget import recommended
        from rockycode.engine.goal_session import LiveGoalBackend

        currency = load_config(self.engine.workdir)["currency"]
        reviewer = os.getenv("ROCKYCODE_REVIEWER_MODEL") or self.engine.model
        return LiveGoalBackend(
            objective, "" if plan_file else self._chat_digest(),
            model=self.engine.model, reviewer_model=reviewer,
            budget=recommended(currency), workdir=self.engine.workdir,
            currency=currency, plan_file=plan_file,
        )

    def _chat_digest(self, max_chars: int = 4000) -> str:
        """A compact recap of the chat for the goal — recent user/assistant turns,
        truncated, so the goal continues what we were doing."""
        parts = []
        for m in self.engine.history:
            role, content = m.get("role"), m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                parts.append(f"{role}: {content.strip()}")
        return "\n".join(parts[-12:])[-max_chars:]

    async def _on_goal_return(self, summary) -> None:
        """Back in chat after the goal screen dismissed. Recap the outcome for the
        user AND seed the model's context so 'review it' / 'keep going' just work."""
        self.query_one(ChatInput).focus()
        if summary is None:
            return
        if summary.status == "cancelled":
            await self._add(Static(f"[{MUTED}]· goal cancelled — nothing ran.[/]", classes="tool-line"))
            return
        colour = VIOLET if summary.status == "done" else AMBER
        recap = f"[bold {colour}]◆ goal {escape(summary.status)}[/] — {escape(summary.reason)}"
        recap += (f"\n  [dim]{summary.milestones_done}/{summary.milestones_total} milestones"
                  f"{f' · branch {escape(summary.branch)}' if summary.branch else ''}[/]")
        if summary.branch:
            recap += (f"\n  [{AMBER}]⚠ the changes are on branch {escape(summary.branch)} in a separate "
                      f"worktree — your current files are untouched.[/]")
            recap += f"\n  [dim]review:[/] [{LAVENDER}]{escape(summary.review_cmd)}[/]"
            recap += f"\n  [dim]merge:[/]  [{LAVENDER}]git -C {escape(summary.origin)} merge {escape(summary.branch)}[/]  [dim](or ask me to)[/]"
        if summary.log:
            recap += f"\n  [dim]log: {escape(summary.log)}[/]"
        await self._add(Static(recap, classes="tool-line"))
        # Seed the model with the exact facts so "review it" / "merge it" work and
        # it never assumes the changes are in the current working tree.
        if summary.branch:
            note = (f'I just ran an autonomous goal: {summary.status} — '
                    f'{summary.milestones_done}/{summary.milestones_total} milestones. The changes are '
                    f'COMMITTED ON BRANCH {summary.branch} in a separate git worktree ({summary.workspace}), '
                    f'NOT in the current working tree — the user\'s current files are unchanged. '
                    f'If they ask to REVIEW it, call the review_goal_branch tool with branch '
                    f'"{summary.branch}". If they ask to MERGE it, call the merge_goal_branch tool with '
                    f'branch "{summary.branch}" — it guards the merge (refuses a dirty tree, aborts on '
                    f'conflict) so use it instead of raw git. Always remind them the work is in a '
                    f'separate worktree.')
        else:
            note = (f'I just ran an autonomous goal: {summary.status} — '
                    f'{summary.milestones_done}/{summary.milestones_total} milestones. {summary.reason}')
        self.engine.history.append({"role": "user", "content": f"[system] {note}"})

    @work(group="sandbox", exclusive=True, exit_on_error=False)
    async def _handle_sandbox(self, text: str) -> None:
        """Handle /sandbox on|off|status.

        A worker (not the message pump): the first `/sandbox on` may pull a
        Docker image, which is unbounded — awaiting it inline froze the UI with
        no way to cancel or quit. Own group so a turn won't cancel a start."""
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub == "status":
            if self.sandbox is not None and self.sandbox.is_running:
                await self._add(
                    Static(
                        f"[{LAVENDER}]sandbox[/] [{VIOLET}]on[/] "
                        f"[dim]({self.sandbox.container_id[:12]}…)[/]",
                        classes="tool-line",
                    )
                )
            else:
                await self._add(Static(f"[{LAVENDER}]sandbox[/] [dim]off — /sandbox on to start[/]", classes="tool-line"))
            return

        if sub == "off":
            if self.sandbox is None or not self.sandbox.is_running:
                await self._add(Static("[dim]sandbox is not running[/]", classes="tool-line"))
                return
            await self.sandbox.stop()
            self.sandbox = None
            if self._local_registry is not None:
                self.engine.swap_registry(self._local_registry)
            # Re-add host-side tools that were stripped during sandbox swap.
            self._restore_host_tools()
            self._render_status()
            await self._add(
                Static(
                    f"[{LAVENDER}]sandbox[/] [dim]stopped — tools back on host filesystem[/]",
                    classes="tool-line",
                )
            )
            await self._maybe_nudge_yolo_host()  # if yolo, the host is exposed again
            return

        if sub == "on":
            if self.sandbox is not None and self.sandbox.is_running:
                await self._add(Static("[dim]sandbox is already running[/]", classes="tool-line"))
                return
            if self._sandbox_starting:
                await self._add(Static("[dim]sandbox is already starting…[/]", classes="tool-line"))
                return
            self._sandbox_starting = True
            try:
                from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry
                want_net = len(parts) > 2 and parts[2].lower() in ("network", "net", "online")
                await self._add(Static(f"[{LAVENDER}]sandbox[/] [dim]starting…[/]", classes="tool-line"))
                sb = await ChatSandbox.start(self.engine.workdir, network=want_net)
                self.sandbox = sb
                host_tools = self._gather_host_tools()
                self.engine.swap_registry(build_sandbox_registry(sb, extras=host_tools))
                self._render_status()
                net_note = (f"[{VIOLET}]network on[/]" if want_net
                            else "[dim]offline (no network) · /sandbox on network to allow[/]")
                await self._add(
                    Static(
                        f"[{LAVENDER}]sandbox[/] [{VIOLET}]on[/] "
                        f"[dim]container {sb.container_id[:12]}… · "
                        f"tools execute in /workspace · [/]{net_note}[dim] · /sandbox off to stop[/]",
                        classes="tool-line",
                    )
                )
            except Exception as exc:
                await self._add(
                    Static(
                        f"[{RED}]sandbox start failed: {exc}[/]",
                        classes="tool-line error-msg",
                    )
                )
            finally:
                self._sandbox_starting = False
            return

        await self._add(Static(f"[{MUTED}]usage: /sandbox on | off | status[/]", classes="tool-line"))

    # ---- input → engine ------------------------------------------------------

    @on(ChatInput.Submitted)
    async def _on_submit(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if text.startswith("!"):
            self._run_shell(text[1:].strip())  # worker; don't block the pump
            return
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        await self._add(Static(f"[bold {VIOLET}]you ▸[/] {escape(text)}", classes="user-msg"))
        self._turn_worker = self._run_turn(text)

    @work(group="shell", exclusive=True, exit_on_error=False)
    async def _run_shell(self, cmd: str) -> None:
        """`! <cmd>`: run a shell command directly (no model round-trip).
        Output shows here AND lands in rocky's context, so the next message
        can refer to it — same semantics as Claude Code's `!` prefix.

        Runs in a worker (not on the message pump): `! npm install` can take
        minutes, and awaiting it inline froze the whole UI — no typing, no
        ctrl+q — until it finished. Its own group so a new turn won't cancel it."""
        if not cmd:
            await self._add(Static(f"[{MUTED}]usage: ! <shell command>[/]", classes="tool-line"))
            return
        if "bash" not in self.engine.registry:
            await self._add(Static("[dim]no bash tool in this session[/]", classes="tool-line"))
            return
        await self._add(Static(f"[bold {AMBER}]$[/] {escape(cmd)}", classes="user-msg"))
        output, _ok = await tools_mod.execute(
            self.engine.registry, "bash", json.dumps({"command": cmd})
        )
        shown = output if len(output) <= 3000 else output[:3000] + "\n… [truncated in view]"
        await self._add(Static(escape(shown), classes="tool-line"))
        # Into history via the engine so the trajectory logs it too.
        self.engine._append(
            {"role": "user", "content": f"[user ran a shell command]\n$ {cmd}\n{output}"}
        )

    @work(group="turn", exclusive=True)
    async def _run_turn(self, text: str) -> None:
        """Drive one turn; turn the modal's Esc (CancelTurn) into a clean stop.
        A worker .cancel() (new submit / Esc binding) is handled in
        on_worker_state_changed — run_turn's finally already keeps history valid."""
        from rockycode.tui.permission import CancelTurn
        try:
            plan_before = self._plan_digest()
            await self._drive_turn(text)
            await self._maybe_plan_gate(plan_before)  # no-op unless plan mode + file changed
            self._maybe_title()  # first completed turn names the session (async, free)
        except CancelTurn:
            await self._flush_buffers()
            await self._finalize_reply()
            self._set_status(AgentState.IDLE)
            await self._add(Static(f"[{MUTED}]· turn cancelled[/]", classes="tool-line"))
            self.query_one(ChatInput).focus()
        except Exception as e:  # noqa: BLE001
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

    async def _drive_turn(self, text: str) -> None:
        self._think_buf = ""
        self._reply_buf = ""
        self._think_flushed = 0
        self._reply_flushed = 0
        self._think_widget = None
        self._think_box = None
        self._reply_widget = None

        async for ev in self.engine.run_turn(text):
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
                        self._think_widget, title="♪ rocky thinking", collapsed=False
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
                await self._add(
                    Static(f"  [{PURPLE}]⚒[/] {ev.tool} …", classes="tool-line")
                )

            elif isinstance(ev, ToolFinished):
                mark = f"[{PURPLE}]✓[/]" if ev.ok else f"[{RED}]✗[/]"
                first = ev.output.strip().splitlines()[0] if ev.output.strip() else ""
                await self._add(
                    Static(
                        f"  {mark} {ev.tool} [dim]({ev.duration_s:.1f}s)[/] [dim]{escape(first[:80])}[/]",
                        classes="tool-line",
                    )
                )
                # Fresh thinking + reply blocks after each tool round, so a
                # post-tool summary lands BELOW the tool output as its own
                # bubble instead of merging into an earlier one.
                await self._reset_step_blocks()

            elif isinstance(ev, Compacted):
                await self._add(
                    Static(
                        f"  [dim]♻ context squeezed: ~{ev.tokens_before:,} → "
                        f"~{ev.tokens_after:,} tokens ({ev.strategy})[/dim]",
                        classes="tool-line",
                    )
                )

            elif isinstance(ev, ContextReminder):
                await self._add(
                    Static(
                        f"  [dim]◐ context at ~{ev.pct:.0%} — DeepSeek V4 is sharpest under half. "
                        f"type [/][{LAVENDER}]/clear[/][dim] to start fresh, or keep going "
                        f"(auto-compacts near full).[/]",
                        classes="tool-line",
                    )
                )

            elif isinstance(ev, EngineError):
                await self._add(Static(f"✗ {escape(ev.message)}", classes="error-msg"))

            elif isinstance(ev, TurnFinished):
                u = ev.usage
                if u:
                    await self._add(
                        Static(
                            f"[dim]· {ev.steps} step(s) · "
                            f"{u.get('prompt_tokens', 0):,} in / "
                            f"{u.get('completion_tokens', 0):,} out · "
                            f"cache hit {u.get('prompt_cache_hit_tokens', 0):,}[/]",
                            classes="usage-line",
                        )
                    )
                    self._ledger.add(self.engine.model, u)
                    self._update_total()

        await self._flush_buffers()
        await self._finalize_reply()

    async def _finalize_reply(self) -> None:
        """Replace the plain streaming Static with a one-shot Markdown render
        when the reply block is done — markdown is parsed once, not per flush."""
        if self._reply_widget is None:
            return
        text = self._reply_buf
        if text.strip():
            # width=0 before first layout → None → links yes, wrapping no.
            md = Markdown(
                enhance_markdown(text, width=self._reply_width(),
                                 only_existing=True),
                parser_factory=rocky_markdown_parser,
                open_links=False,
            )
            await self._transcript().mount(md, after=self._reply_widget)
            await self._reply_widget.remove()
        self._reply_buf = ""
        self._reply_flushed = 0
        self._reply_widget = None

    async def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Clicks are for checking things — the paper, the docs, the web —
        never for running things. Text docs dock beside the chat, other
        reading files open with the system default; code and everything else
        get a dim note (open_links=False everywhere, so this is the only
        click path — including for links inside a docked doc)."""
        href = event.href
        dock = self._dock()
        if href.startswith("#"):  # heading anchor inside the docked doc
            if dock is not None:
                dock.query_one("#docdock-md", Markdown).goto_anchor(href[1:])
            return
        if (dock is not None and dock.current is not None
                and not urlsplit(href).scheme and not href.startswith("/")):
            # a docked doc's relative link ("figs/x.md") — resolve against the
            # doc, then through the same policy gate as every other click
            href = str((dock.current.parent / url2pathname(href)).resolve())
        action, target = link_click_action(href)
        if action == "open" and Path(urlsplit(target).path).suffix.lower() in TEXT_DOC_SUFFIXES:
            await self._open_doc(target)
        elif action in ("browser", "open"):
            self.open_url(target)
        elif action == "missing":
            await self._add(Static(
                f"[dim]· link target missing (moved or renamed?): {escape(target)}[/]",
                classes="tool-line"))
        else:
            await self._add(Static(
                f"[dim]· not opened by click — only reading files (md/pdf/…) and "
                f"web links auto-open: {escape(target)}[/]",
                classes="tool-line"))

    # ---- doc dock: read beside the chat, never over it ----------------------

    def _dock(self) -> DocDock | None:
        try:
            return self.query_one(DocDock)
        except NoMatches:
            return None

    def _reply_width(self) -> int | None:
        """Fence-wrap width for replies: the TRANSCRIPT's width, not the
        app's — with a doc docked the chat column is narrower. 0 before
        first layout → None → links yes, wrapping no."""
        return self._transcript().container_size.width or self.size.width or None

    async def _open_doc(self, uri: str) -> None:
        path = Path(url2pathname(urlsplit(uri).path))
        dock = self._dock()
        if dock is None:
            dock = DocDock()
            await self.query_one("#workspace").mount(dock)
        await dock.load(path)

    async def action_doc_close(self) -> None:
        dock = self._dock()
        if dock is not None:
            self._transcript().display = True  # in case it closed while ⛶ full
            await dock.remove()

    async def action_doc_back(self) -> None:
        dock = self._dock()
        if dock is not None:
            await dock.back()

    def action_doc_wider(self) -> None:
        dock = self._dock()
        if dock is not None and not dock.is_full:
            dock.width_i = (dock.width_i + 1) % len(WIDTHS)
            dock.styles.width = WIDTHS[dock.width_i]

    def action_doc_full(self) -> None:
        dock = self._dock()
        if dock is None:
            return
        dock.is_full = not dock.is_full
        self._transcript().display = not dock.is_full
        dock.styles.width = "100%" if dock.is_full else WIDTHS[dock.width_i]

    def _update_total(self) -> None:
        # Price directly in the chosen currency from its own official table —
        # no usd→cny conversion (see pricing.py). Peak surcharge is applied per
        # request inside cost().
        amt = self._ledger.cost(self._currency)
        sym = "¥" if self._currency == "cny" else "$"
        note = "" if self._ledger.configured(self._currency) else " [dim](prices unset)[/]"
        t = self._ledger.totals()
        rate = (t["hit"] / t["prompt"] * 100) if t["prompt"] else 0.0
        self.query_one("#total", Static).update(
            f"[{MUTED}]Σ {sym}{amt:.4f} · {t['prompt']:,} in · "
            f"{t['completion']:,} out · cache {rate:.0f}%{note}[/]"
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
    currency: str = "usd", theme: str = "dark",
    permission: str = "ask", permission_weakened: bool = False,
    exit_sheet: str = "auto", dream: str = "auto",
):
    """Run the chat TUI. /goal runs in its own screen inside the app and pops back,
    so this just runs the app to normal exit."""
    return RockyCodeApp(
        engine, resume=resume, resume_session=resume_session, sandbox=sandbox,
        currency=currency, theme=theme,
        permission=permission, permission_weakened=permission_weakened,
        prompt_history=PromptHistory(),  # the real CLI persists; tests stay in-memory
        exit_sheet=exit_sheet, dream=dream,
    ).run()
