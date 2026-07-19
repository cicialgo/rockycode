"""Rocky the pet: a small frame-based sprite widget driven by AgentState.

Eridian anatomy: Rocky has FIVE limbs arranged radially — drawn in the
兴 pose (three limbs up/out over the carapace, two below), never five-down
like a spider. Carapace carries sonar dots (Rocky has no eyes — he hears).
A more delicate sprite pass is still planned (user request, 2026-06-10).

Colors are Tokyo Night palette, matching the app theme.

The widget is isolated on purpose: the animation timer repaints only this
~9×6 cell region, so it can never glitch the transcript.
"""
from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from rockycode.engine.events import AgentState

# Every frame: 6 lines × 7 cols. 5 limbs (3 over carapace, 2 under),
# carapace with sonar dots, last line = caption.
FRAMES: dict[AgentState, list[str]] = {
    AgentState.IDLE: [
        " ╲ │ ╱ \n╭─────╮\n│ ∙∙∙ │\n╰─────╯\n ╱   ╲ \n       ",
        " ╲ │ ╱ \n╭─────╮\n│ ∘∙∘ │\n╰─────╯\n  ╱ ╲  \n       ",
    ],
    AgentState.THINKING: [
        " ╲ │ ♪ \n╭─────╮\n│ ∙∘∙ │\n╰─────╯\n ╱   ╲ \n hmm…  ",
        " ╲ │ ♫ \n╭─────╮\n│ ∘∙∘ │\n╰─────╯\n ╱   ╲ \n hmm…  ",
        " ╲ │ ♪ \n╭─────╮\n│ ∘∘∘ │\n╰─────╯\n ╱   ╲ \n hmm…  ",
    ],
    AgentState.RESPONDING: [
        " ╲ │ ╱ \n╭─────╮\n│ ∙∙∙ │\n╰─────╯\n ╱   ╲ \n  ♪    ",
        " ╲ │ ╱ \n╭─────╮\n│ ∙∙∙ │\n╰─────╯\n ╱   ╲ \n    ♫  ",
    ],
    AgentState.TOOL: [
        " ╲ │ ⚒ \n╭─────╮\n│ ∙∙∙ │\n╰─────╯\n ╱   ╲ \n work… ",
        " ╲ │ ╱ \n╭────⚒╮\n│ ∙∙∙ │\n╰─────╯\n ╱   ╲ \n work… ",
    ],
    AgentState.COMPACTING: [
        " ╲ │ ╱ \n╭─────╮\n│ >∙< │\n╰─────╯\n ╱   ╲ \nsquish…",
        " ╲ │ ╱ \n╭─────╮\n│ »∙« │\n╰─────╯\n ╱   ╲ \nsquish…",
    ],
    AgentState.AMAZED: [
        "╲  │  ╱\n╭─────╮\n│ ✧✧✧ │\n╰─────╯\n╱     ╲\namaze! ",
        " ╲ │ ╱ \n╭─────╮\n│ ✧∙✧ │\n╰─────╯\n ╱   ╲ \namaze! ",
        "╲  │  ╱\n╭─────╮\n│ ∙✧∙ │\n╰─────╯\n╱     ╲\namaze! ",
    ],
    AgentState.ERROR: [
        " ╲ ? ╱ \n╭─────╮\n│ ××× │\n╰─────╯\n ╳   ╲ \nno good",
        " ╲ ¿ ╱ \n╭─────╮\n│ ××× │\n╰─────╯\n ╱   ╳ \nno good",
    ],
}

# Tokyo Night.
STYLES: dict[AgentState, str] = {
    AgentState.IDLE: "#bb9af7",
    AgentState.THINKING: "#9d7cd8",
    AgentState.RESPONDING: "#7dcfff",
    AgentState.TOOL: "#e0af68",
    AgentState.COMPACTING: "#73daca",
    AgentState.AMAZED: "bold #7dcfff",
    AgentState.ERROR: "#f7768e",
}

FPS = 3  # a pet, not a video game — cheap repaints


class RockyPet(Static):
    state: reactive[AgentState] = reactive(AgentState.IDLE)

    DEFAULT_CSS = """
    RockyPet {
        width: 9;
        height: 6;
        content-align: center middle;
    }
    """

    def on_mount(self) -> None:
        self._frame = 0
        self.set_interval(1 / FPS, self._tick)
        self._render_frame()

    def watch_state(self, _old: AgentState, _new: AgentState) -> None:
        self._frame = 0
        self._render_frame()

    def _tick(self) -> None:
        self._frame += 1
        self._render_frame()

    def _render_frame(self) -> None:
        frames = FRAMES[self.state]
        art = frames[self._frame % len(frames)]
        self.update(f"[{STYLES[self.state]}]{art}[/]")
