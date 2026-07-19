"""ChatInput arrow-key navigation (headless pilot, no API).

Up/Down move the cursor *within* multi-line and soft-wrapped text, and only
reach into input history from the top/bottom VISUAL row. The bug this guards:
a long single line that soft-wraps has one logical line, so the old
`cursor_location[0] == 0` check recalled history on the first up-press instead
of moving to the upper wrapped row. The fix counts visual rows via
wrapped_document, so:
  - blank input  + up            → previous message (history)
  - multi-line   + up mid-text   → move up a row (text unchanged)
  - top row      + up            → previous message, current text kept as draft
  - wrapped line + up from bottom→ move up a wrapped row (NOT history)
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyinput-"))

from rockycode.engine.loop import Engine
from rockycode.tui.app import ChatInput, RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def wait_until(pilot, cond, timeout=4.0, step=0.05):
    """Poll until cond() or timeout — robust to CPU load (a fixed pause before an
    assert races when the whole suite runs the pilot under load)."""
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def main():
    app = build_app()
    async with app.run_test(size=(90, 30)) as pilot:
        inp = app.query_one(ChatInput)
        inp.focus()
        inp._history = ["first old msg", "second old msg"]
        await pilot.pause(0.1)

        # 1) BLANK input: up recalls the most-recent history item.
        inp._hist_idx = None
        inp.text = ""
        await pilot.pause(0.05)
        await pilot.press("up")
        assert await wait_until(pilot, lambda: inp.text == "second old msg"), \
            f"blank+up should recall history, got {inp.text!r}"

        # 2) MULTI-LINE input: up moves a row while there's text above; only the
        #    top visual row reaches history — and saves the draft.
        inp._hist_idx = None
        inp.text = "aaa\nbbb\nccc"
        inp.move_cursor(inp.document.end)            # row 2 (bottom)
        await pilot.pause(0.05)
        await pilot.press("up")                      # -> row 1, text unchanged
        assert await wait_until(pilot, lambda: inp.cursor_location[0] == 1), \
            f"cursor should be on row 1, got {inp.cursor_location}"
        assert inp.text == "aaa\nbbb\nccc", f"up must not recall mid-text, got {inp.text!r}"
        await pilot.press("up")                      # -> row 0 (top), text unchanged
        assert await wait_until(pilot, lambda: inp.cursor_location[0] == 0), \
            f"cursor should be on row 0, got {inp.cursor_location}"
        assert inp.text == "aaa\nbbb\nccc", f"up to top row must not recall, got {inp.text!r}"
        await pilot.press("up")                      # top visual row -> history
        assert await wait_until(pilot, lambda: inp.text == "second old msg"), \
            f"up at top row should recall history, got {inp.text!r}"
        assert inp._draft == "aaa\nbbb\nccc", f"draft must be saved, got {inp._draft!r}"

        # down from the (single-row) history item returns forward to the draft.
        await pilot.press("down")
        assert await wait_until(pilot, lambda: inp.text == "aaa\nbbb\nccc"), \
            f"down should restore the draft, got {inp.text!r}"

        # 3) SOFT-WRAPPED single logical line: up from the end moves within the
        #    wrapped rows, NOT into history. This is the regression the fix cures.
        inp._hist_idx = None
        long_line = "x" * 400                        # wraps to many rows at width 90
        inp.text = long_line
        inp.move_cursor(inp.document.end)
        await pilot.pause(0.05)
        _, vtotal = inp._cursor_visual_row()
        assert vtotal > 1, f"setup: line should soft-wrap, visual height={vtotal}"
        await pilot.press("up")
        await pilot.pause(0.05)
        assert inp.text == long_line, \
            f"up on a wrapped line must move within it, not recall history (got {inp.text[:24]!r}...)"

    print("TUI INPUT-NAV SMOKE OK — up/down move within wrapped text; history only at top/bottom row. amaze!")


asyncio.run(main())
