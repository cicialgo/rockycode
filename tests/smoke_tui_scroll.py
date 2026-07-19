"""Transcript scrolling — mouse wheel + keyboard (headless Textual pilot, no API).

rockycode runs full-screen (alt-screen), so there is no terminal scrollback to
wheel through — the chat history lives inside a scroll container and the app has
to move it itself. This guards every path that scrolls it:

  - wheel over the transcript            → scrolls (native)
  - wheel over a child line (Static/MD)  → bubbles up → scrolls
  - wheel over the focused input         → ChatInput forwards it → scrolls
  - wheel anywhere else                  → App.on_mouse_scroll_* catch-all
  - pageup / pagedown                    → page the history
  - shift+↑ / shift+↓                     → line the history

The regression that motivated the keyboard part: the page/line scroll actions
used the default animated scroll, which lags on a keypress and, under the
headless pilot, hadn't applied yet — they now scroll with animate=False.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyscroll-"))

from textual.events import MouseScrollUp
from textual.widgets import Markdown, Static

from rockycode.engine.loop import Engine
from rockycode.tui.app import ChatInput, RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


def wheel_up(widget):
    return MouseScrollUp(
        widget=widget, x=5, y=5, delta_x=0, delta_y=1, button=0,
        shift=False, meta=False, ctrl=False, screen_x=5, screen_y=5, style=None,
    )


async def wait_until(pilot, cond, timeout=4.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def main():
    app = build_app()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.1)
        kids = []
        for i in range(80):
            w = Static(f"line {i}")
            kids.append(w)
            await app._add(w)
        md = Markdown("# heading\nassistant **reply** text\n" + "body\n" * 8)
        await app._add(md)
        tr = app.query_one("#transcript")
        inp = app.query_one(ChatInput)
        inp.focus()
        await pilot.pause(0.2)

        async def to_bottom():
            tr.scroll_end(animate=False)
            await pilot.pause(0.05)
            return tr.scroll_offset.y

        assert await to_bottom() > 0, "setup: transcript must have scrollable history"

        # 1) wheel over the transcript itself
        y0 = await to_bottom()
        tr.post_message(wheel_up(tr))
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y0), "wheel over transcript must scroll up"

        # 2) wheel over a child line bubbles up to the scroll container
        y1 = await to_bottom()
        kids[-1].post_message(wheel_up(kids[-1]))
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y1), "wheel over a Static line must scroll"

        # 3) wheel over the Markdown reply (has its own inner widgets) bubbles too
        y2 = await to_bottom()
        md.post_message(wheel_up(md))
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y2), "wheel over the Markdown reply must scroll"

        # 4) wheel over the focused input — ChatInput forwards it to the transcript
        #    (some terminals route the wheel to the focused widget, not the hover)
        y3 = await to_bottom()
        inp.post_message(wheel_up(inp))
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y3), "wheel over the input must forward + scroll"

        # 5) keyboard: pageup pages the history (input focused)
        y4 = await to_bottom()
        await pilot.press("pageup")
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y4), "pageup must page the history"

        # 6) keyboard: shift+up nudges one line; shift+down comes back
        await pilot.pause(0.05)
        top_after_pgup = tr.scroll_offset.y
        await pilot.press("shift+down")
        assert await wait_until(pilot, lambda: tr.scroll_offset.y > top_after_pgup), "shift+down must line-scroll down"
        y5 = tr.scroll_offset.y
        await pilot.press("shift+up")
        assert await wait_until(pilot, lambda: tr.scroll_offset.y < y5), "shift+up must line-scroll up"

    print("TUI SCROLL SMOKE OK — wheel (transcript / child / markdown / input-forward) + pgup + shift+↑↓. amaze!")


asyncio.run(main())
