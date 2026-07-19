"""The exit feedback sheet — one tiny, skippable question on the way out.

Mounted inline (above the input, like InlineApproval) when the user leaves via
/exit, /quit or ctrl+q after a real exchange. Resolves the passed-in Future
with {"mood", "text"} or None (skipped); the caller writes it as a `feedback`
trajectory record. That record stays ON THIS MACHINE: it is never placed in a
cloud-bound prompt — only the local dream (Ollama) reads it later — and the
sheet says so right on it (the self-evolve design's disclosure line).

Everything is clickable (house rule: a keystroke is never the only way):
click a mood to send it, click skip to leave silently, click "don't ask
again" to persist exit_sheet=off. Keys are paired accelerators — ←→ move the
highlight, Enter sends it with the typed note, Esc skips.

The sheet never holds the exit hostage: after TIMEOUT_S with no answer it
auto-skips and the app leaves as requested (the caller sees None, exactly
like a click on skip). Config `exit_sheet`: the default "auto" shows it only
when the dream pipeline is alive this session (the feedback has a real local
consumer); "off" suppresses it entirely; "on" forces it.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Union

from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from rockycode.palette import LAVENDER, MUTED, VIOLET

# One glance, one click — if it hasn't been answered in a minute it won't be,
# and a typed /exit must actually exit (the outcome record + exit card wait
# on this). Deliberately short; the sheet is optional, the exit is not.
TIMEOUT_S = 60.0

MOODS = (
    ("good", "♥ good"),
    ("meh", "· okay"),
    ("bad", "✗ rough"),
)

NEVER = "never"  # sentinel result: the user clicked "don't ask again"


class _MoodChip(Static):
    """One clickable mood. A click picks it and sends the sheet."""

    def __init__(self, mood: str, sheet: "ExitSheet") -> None:
        super().__init__("", id=f"sheet-mood-{mood}", classes="sheet-chip")
        self._mood = mood
        self._sheet = sheet

    def on_click(self) -> None:
        self._sheet.pick(self._mood)


class _SkipChip(Static):
    def __init__(self, sheet: "ExitSheet") -> None:
        super().__init__(f"[{MUTED}]skip →[/]", id="sheet-skip")
        self._sheet = sheet

    def on_click(self) -> None:
        self._sheet.skip()


class _NeverChip(Static):
    """The off-switch lives exactly where the annoyance is — no docs hunt."""

    def __init__(self, sheet: "ExitSheet") -> None:
        super().__init__(f"[{MUTED}]don't ask again[/]", id="sheet-never")
        self._sheet = sheet

    def on_click(self) -> None:
        self._sheet.never()


class ExitSheet(Vertical):
    """Rate the session on the way out. Resolves `future` with
    {"mood": "good"|"meh"|"bad", "text": str}, None when skipped (click,
    Esc, or timeout), or NEVER when the user turns the sheet off for good."""

    can_focus = True

    BINDINGS = [
        ("left", "move(-1)", "left"),
        ("right", "move(1)", "right"),
        ("enter", "send", "send"),
        ("escape", "skip", "skip"),
    ]

    DEFAULT_CSS = """
    ExitSheet {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
    }
    ExitSheet #sheet-moods { height: 1; margin-bottom: 1; }
    ExitSheet .sheet-chip { width: auto; margin-right: 4; }
    ExitSheet #sheet-never { width: 1fr; text-align: right; }
    ExitSheet #sheet-skip { width: auto; margin-left: 4; }
    ExitSheet #sheet-privacy { color: $text-muted; margin-top: 1; }
    ExitSheet #sheet-keys { color: $text-muted; }
    """

    def __init__(
        self,
        future: "asyncio.Future[Union[dict, str, None]]",
        timeout_s: Optional[float] = None,
    ) -> None:
        super().__init__()
        self._future = future
        # Resolved at call time so tests (and a future config knob) can adjust.
        self._timeout_s = TIMEOUT_S if timeout_s is None else timeout_s
        self._idx = 1  # default highlight = the neutral middle, never presume

    def compose(self):
        with Horizontal(id="sheet-moods"):
            for mood, _label in MOODS:
                yield _MoodChip(mood, self)
            yield _NeverChip(self)
            yield _SkipChip(self)
        yield Input(placeholder="a note for rocky's dream (optional)", id="sheet-note")
        yield Static(
            f"[{MUTED}]feeds rocky's dream (local memory) — never sent to the model API · "
            f"turn off anytime: “don't ask again” here, or /config exit_sheet off[/]",
            id="sheet-privacy",
        )
        yield Static(
            f"[{LAVENDER}]←→[/] choose · [{LAVENDER}]↵[/] send · "
            f"[{LAVENDER}]esc[/] skip · or just click a mood",
            id="sheet-keys",
        )

    def on_mount(self) -> None:
        self.border_title = "♪ before you go — how was this session?"
        self.border_subtitle = f"optional · one click · auto-skips in {self._timeout_s:.0f}s"
        self._render_moods()
        self.focus()
        # Never hold the exit hostage: unanswered → auto-skip, app leaves.
        self.set_timer(self._timeout_s, self.skip)

    def _render_moods(self) -> None:
        for i, (mood, label) in enumerate(MOODS):
            chip = self.query_one(f"#sheet-mood-{mood}", Static)
            chip.update(f"[b {VIOLET}]▸ {label}[/]" if i == self._idx else f"[{MUTED}]  {label}[/]")

    def action_move(self, delta: int) -> None:
        self._idx = (self._idx + delta) % len(MOODS)
        self._render_moods()

    def action_send(self) -> None:
        self.pick(MOODS[self._idx][0])

    def action_skip(self) -> None:
        self.skip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter inside the note box sends the note with the highlighted mood.
        event.stop()
        self.action_send()

    def pick(self, mood: str) -> None:
        text = self.query_one("#sheet-note", Input).value.strip()
        self._resolve({"mood": mood, "text": text})

    def skip(self) -> None:
        self._resolve(None)

    def never(self) -> None:
        self._resolve(NEVER)

    def _resolve(self, value: "Union[dict, str, None]") -> None:
        """Deliver the answer to the awaiting exit path. Idempotent — a stray
        second click/key (or the timeout firing after a click) must not crash
        on an already-set Future."""
        if not self._future.done():
            self._future.set_result(value)
