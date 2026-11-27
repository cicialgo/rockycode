"""The doc dock: open a linked paper or doc beside the chat, never over it.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Markdown, Static


from rockycode.tui.mdterm import (
    link_click_action,
)
from rockycode.tui.mdview import TEXT_DOC_SUFFIXES, WIDTHS, DocDock
from rockycode.tui.chatinput import ChatInput


class DocsMixin:
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
                self._t('app.link_target_missing_moved_or_renamed', p0=escape(target)),
                classes="tool-line"))
        else:
            await self._add(Static(
                self._t('app.doc_link_not_opened', p0=escape(target)),
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
            dock = DocDock(language=self._language)
            await self.query_one("#workspace").mount(dock)
        await dock.load(path)

    def action_doc_close(self) -> None:
        """Sync on purpose: the ✕ lives in the dock's own header, so this
        action runs on the clicked widget's message pump. Awaiting remove()
        there gathers the dock's child pumps — including the header's own,
        which is blocked on this very handler — and the app wedges for good.
        Schedule the removal, hand focus back, return."""
        dock = self._dock()
        if dock is not None:
            self._transcript().display = True  # in case it closed while ⛶ full
            dock.remove()
            self.query_one(ChatInput).focus()

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
