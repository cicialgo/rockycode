"""Select-and-copy: drag over transcript text, release, it's in the clipboard.

Textual 8.x provides click-drag selection (ALLOW_SELECT) and OSC-52 copy;
rocky wires the last mile — mouse-up with a selection auto-copies, clears
the selection (no silent re-clobber later), and confirms with a toast.
Headless pilot; the selection itself is injected (pilot can't drag)."""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockycopy-"))

from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def main():
    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        cleared = []
        piped = []
        app.screen.get_selected_text = lambda: "mathtex xxx thing"
        app.clear_selection = lambda: cleared.append(True)
        app._pbcopy = lambda text: piped.append(text) or True  # never the real clipboard in tests

        app.on_mouse_up(types.SimpleNamespace())
        assert app.clipboard == "mathtex xxx thing", repr(app.clipboard)
        assert piped == ["mathtex xxx thing"], "pbcopy path must be attempted (iTerm2 blocks OSC 52)"
        assert cleared, "selection must be cleared after the copy"
        print("copy: release with selection → clipboard + cleared  ✓")

        # no selection (a plain click, e.g. on a link) → clipboard untouched
        app.screen.get_selected_text = lambda: None
        app.on_mouse_up(types.SimpleNamespace())
        assert app.clipboard == "mathtex xxx thing", "plain clicks must not touch the clipboard"
        print("copy: selection-less mouse-up is a no-op  ✓")

        # transcript widgets must remain selectable (the machinery this rides on)
        from textual.widgets import Markdown, Static
        assert Static.ALLOW_SELECT and Markdown.ALLOW_SELECT
        print("copy: transcript widgets allow selection  ✓")


asyncio.run(main())
print("TUI COPY SMOKE OK — three words, drag, done. amaze!")
