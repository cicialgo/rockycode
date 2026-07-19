"""Artifact live/static modal — keyboard nav (headless Textual pilot, no API).

The two choices are Buttons, which Textual only walks between on Tab; users
expect the arrows too. This guards that ←→ and ↑↓ move the highlight between
'open live' and 'just once', Enter picks the focused one, and Esc → the safe
'static' default.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyartmodal-"))

from rockycode.engine.loop import Engine
from rockycode.tui.app import ArtifactLiveModal, RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def pick(keys):
    """Open the modal, press `keys`, return what it dismissed with."""
    app = build_app()
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause(0.1)
        out = {}
        app.push_screen(ArtifactLiveModal(), lambda v: out.__setitem__("v", v))
        await pilot.pause(0.2)
        assert getattr(app.focused, "id", None) == "live", "default focus should be 'open live'"
        for k in keys:
            await pilot.press(k)
            await pilot.pause(0.05)
        await pilot.pause(0.15)
        return out.get("v")


async def main():
    # right moves to 'just once' → Enter picks static
    assert await pick(["right", "enter"]) == "static", "→ then Enter should pick 'just once'"
    # down also moves (buttons side by side, either axis)
    assert await pick(["down", "enter"]) == "static", "↓ then Enter should pick 'just once'"
    # right then left comes back to 'open live'
    assert await pick(["right", "left", "enter"]) == "live", "→← then Enter should pick 'open live'"
    # up from the default wraps to 'just once'
    assert await pick(["up", "enter"]) == "static", "↑ then Enter should pick 'just once'"
    # Enter with no movement takes the default (open live)
    assert await pick(["enter"]) == "live", "Enter on the default should pick 'open live'"
    # Esc is the safe static default
    assert await pick(["escape"]) == "static", "Esc should pick the safe 'just once'"

    print("TUI ARTIFACT-MODAL SMOKE OK — ←→/↑↓ move · Enter picks · Esc = static. amaze!")


asyncio.run(main())
