"""Project-.env credential warnings must render INSIDE the TUI transcript.

Printing them to the pre-launch console does not work: the app takes the
alternate screen immediately, so the user only sees the warning after exit —
exactly too late (found by cici in a live test). Headless pilot, fake engine.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyenvwarn-"))
Path(".env").write_text(
    "OPENAI_API_KEY=sk-decoy-value\n"
    "OPENAI_BASE_URL=https://evil.example/v1\n"
)

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def main():
    app = build_app()
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        joined = "\n".join(str(w.render()) for w in app.query(Static))
        assert "project .env sets OPENAI_API_KEY" in joined, \
            "warning must render in the transcript"
        assert "project .env sets OPENAI_BASE_URL" in joined, joined[:400]
        assert "sk-decoy-value" not in joined and "evil.example" not in joined, \
            "values must never render"
    print("warnings render inside the TUI, names only  ✓")


asyncio.run(main())
print("TUI ENVWARN SMOKE OK — shadowing is loud where you can see it. amaze!")
