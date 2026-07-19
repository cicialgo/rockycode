"""The doc dock — read a paper or doc BESIDE the chat, never over it.

Clicking a text-family link (see TEXT_DOC_SUFFIXES) docks the rendered file
in a right-hand pane instead of launching an external app: the transcript
stays live and visible, scrolls independently, and gets its exact width back
on close. Nothing here ever touches engine history — the dock is pure
display, so the prompt-cache prefix is byte-identical with or without it.

The click policy stays upstream: the app routes a file here only after
mdterm.link_click_action returned "open" (resolved regular file, reading
suffix, no exec bit). The dock can only render — it has no way to run.
"""
from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static

from rockycode.palette import MUTED
from rockycode.tui.mdterm import rocky_markdown_parser

# What docks in-app: files a terminal can honestly render. Everything else
# that passed the click policy (pdf, images, html) still goes to `open`.
TEXT_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}

READ_CAP = 200_000  # chars — keeps a giant log paste from freezing the dock
WIDTHS = ("45%", "62%", "30%")  # ⇄ cycles; the CSS default matches WIDTHS[0]


def read_doc(path: Path, cap: int = READ_CAP) -> str:
    """File → markdown for the dock. .txt is fenced so it stays verbatim;
    oversized files are cut at `cap` chars with an honest tail note."""
    text = path.read_text(errors="replace")
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]
    if path.suffix.lower() == ".txt":
        text = f"````text\n{text}\n````"
    if truncated:
        text += f"\n\n> ✂ showing the first {cap:,} characters — the file has more."
    return text


class DocDock(Vertical):
    """Right-hand reading pane. Keeps a back-stack so links inside a docked
    doc (routed through the same click policy by the app) navigate in place."""

    def __init__(self) -> None:
        super().__init__(id="docdock")
        self._stack: list[Path] = []
        self.width_i = 0
        self.is_full = False

    @property
    def current(self) -> Path | None:
        return self._stack[-1] if self._stack else None

    def compose(self):
        yield Static("", id="docdock-head")
        yield VerticalScroll(
            Markdown("", parser_factory=rocky_markdown_parser,
                     open_links=False, id="docdock-md"),
            id="docdock-body",
        )

    async def load(self, path: Path) -> None:
        self._stack.append(path)
        await self._show()

    async def back(self) -> bool:
        if len(self._stack) < 2:
            return False
        self._stack.pop()
        await self._show()
        return True

    async def _show(self) -> None:
        path = self._stack[-1]
        await self.query_one("#docdock-md", Markdown).update(read_doc(path))
        self.border_title = f"♪ {escape(path.name)}"
        back = "[@click=app.doc_back]← back[/] · " if len(self._stack) > 1 else ""
        self.query_one("#docdock-head", Static).update(
            f"[{MUTED}]{back}[@click=app.doc_wider]⇄ width[/] · "
            f"[@click=app.doc_full]⛶ full[/] · "
            f"[@click=app.doc_close]✕ close · esc[/] · {escape(str(path))}[/]"
        )
        self.query_one("#docdock-body", VerticalScroll).scroll_home(animate=False)

    # Wheel over the header/border must scroll the DOC, not fall through to
    # the app-level catch-all that scrolls the transcript.
    def on_mouse_scroll_up(self, event) -> None:
        self.query_one("#docdock-body", VerticalScroll).scroll_relative(y=-3, animate=False)
        event.stop()

    def on_mouse_scroll_down(self, event) -> None:
        self.query_one("#docdock-body", VerticalScroll).scroll_relative(y=3, animate=False)
        event.stop()
