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

from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.artifact import ArtifactRegistry  # noqa: E402
from rockycode.tui.app import ArtifactLiveModal, RockyCodeApp  # noqa: E402
from textual.widgets import Static  # noqa: E402


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


async def check_session_inventory():
    app = build_app()
    registry = ArtifactRegistry()
    registry.upsert(
        name="Review", title="Review", path=Path.cwd() / "Review.html",
        url=(Path.cwd() / "Review.html").as_uri(), live=False,
    )
    app.engine.artifact_registry = registry
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause(0.1)
        app._render_cwd()
        assert "1 artifact" in str(app.query_one("#cwd", Static).render()), \
            "session footer must show the Artifact count"
        await app._handle_artifact("/artifact list")
        transcript = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "Review" in transcript and "static" in transcript, transcript


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

    await check_session_inventory()

    print("TUI ARTIFACT SMOKE OK — modal keys + session inventory badge/list. amaze!")


asyncio.run(main())
