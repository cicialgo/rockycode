"""The first hour in the TUI: small things a new user trips on (headless pilot, no API).

- typing a slash command: the suggestion row follows every keystroke, a bare
  "/" lists commands, Tab completes and never throws the focus away;
- /help shows its arguments ([objective], [key] [value]) and opens at its top;
- /clear says what it did — the screen is empty, the conversation is not;
- something moves the moment a message is sent, before the first token;
- the hint bar fits an 80-column terminal, and the goal screen speaks the
  session's language.
"""
import asyncio
import os
import re
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyfirsthour-"))
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

from rich.text import Text  # noqa: E402
from textual.widgets import Static  # noqa: E402

from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.i18n import PALETTE, tr  # noqa: E402
from rockycode.tui import commands  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402

ROOT = _HERE.parent / "rockycode"


def plain(markup: str) -> str:
    return Text.from_markup(markup).plain


class Completions:
    """Blocks inside create() until released: the wait before the first token."""

    def __init__(self):
        self.release = asyncio.Event()

    async def create(self, **kwargs):
        await self.release.wait()

        async def stream():
            delta = types.SimpleNamespace(reasoning_content=None, content="done.", tool_calls=None)
            yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")])
        return stream()


async def main():
    completions = Completions()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    engine = Engine(model="deepseek-flash", client=client, workdir=Path.cwd())
    engine.evolve_enabled = False
    app = RockyCodeApp(engine, permission="yolo")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one(ChatInput)
        hints = app.query_one("#hints", Static)

        def row() -> str:
            return str(hints.render())

        # ── the hint bar: whole at 80 columns, and it names the way to the rest ──
        bar = plain(tr("app.hints_bar", "en"))
        assert len(bar) <= 78, (len(bar), bar)
        for token in ("/help", "/model", "/plan", "/goal", "/exit", "type / for more"):
            assert token in bar and token in row(), token
        assert len(plain(tr("app.hints_bar", "zh"))) <= 60, "CJK cells are double width"
        print("hints: the bar fits 80 columns and ends with /exit and `type / for more`  ✓")

        # ── typing a slash command ───────────────────────────────────────────────
        box.focus()
        box.text = "/"
        await pilot.pause()
        assert "/help" in row() and "/plan" in row() and "more — /help" in row(), row()
        box.text = "/p"
        await pilot.pause()
        first = row()
        assert "/plan" in first and "/paste" in first and "/help" not in first, first
        box.text = "/pe"
        await pilot.pause()
        assert "/permission" in row() and "/plan" not in row(), "the row follows the next keystroke"
        box.text = "/plax"
        await pilot.pause()
        assert "no match" in row(), row()
        box.text = ""
        await pilot.pause()
        assert "type / for more" in row(), "and the bar comes back"
        # Tab: several matches → as far as they agree; one → the command; never a focus jump.
        box.text = "/pr"
        box.move_cursor(box.document.end)
        await pilot.press("tab")
        await pilot.pause()
        assert box.text == "/pro" and app.focused is box, (box.text, app.focused)
        box.text = "/mod"
        box.move_cursor(box.document.end)
        await pilot.press("tab")
        await pilot.pause()
        assert box.text == "/model " and app.focused is box
        box.text = "plain words"
        box.move_cursor(box.document.end)
        await pilot.press("tab")
        await pilot.press("x")
        await pilot.pause()
        assert app.focused is box and box.text == "plain wordsx", (app.focused, box.text)
        box.text = ""
        print("slash: the row follows each keystroke, a bare / lists, Tab completes and keeps the focus  ✓")

        # ── something moves the moment a message is sent ─────────────────────────
        status = app.query_one("#status", Static)
        assert "thinking" not in str(status.render())
        box.text = "hi"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "thinking" in str(status.render()), str(status.render())
        completions.release.set()
        for _ in range(60):
            await pilot.pause(0.05)
            if "thinking" not in str(status.render()):
                break
        assert "thinking" not in str(status.render())
        print("status: thinking shows before the first token arrives  ✓")

        # ── /help: its arguments are visible, and it opens at its first line ──────
        await commands.dispatch(app, "/help")
        await pilot.pause(0.3)
        card = next(w for w in app.query(Static) if "/goal" in str(w.render()) and "/queue" in str(w.render()))
        text = str(card.render())
        assert "/goal [objective]" in text and "/config [key] [value]" in text, text[:400]
        assert "/effort [off|low|high|max]" in text and "/permission [yolo|ask|careful]" in text
        transcript = app._transcript()
        assert card.region.height > transcript.size.height, "the card is taller than this terminal"
        assert abs(card.region.y - transcript.region.y) <= 1, (card.region, transcript.region, transcript.scroll_offset)
        assert "[id]" in plain(tr("app.loop_help", "en", **PALETTE)), "the loop help keeps its [id]"
        print("help: [objective], [key] [value] and [id] are shown; the card opens at its top  ✓")

        # ── /clear: the screen, not the conversation — and it says so ────────────
        before = len(engine.history)
        await commands.dispatch(app, "/clear")
        await pilot.pause(0.2)
        lines = [str(w.render()) for w in app._transcript().query(Static)]
        assert len(lines) == 1 and "rocky still remembers" in lines[0] and "/task new" in lines[0], lines
        assert len(engine.history) == before and before >= 3
        reminder = plain(tr("app.context_reminder", "en", p0=0.5, p1="1M", **PALETTE))
        assert "/clear" not in reminder and "/task new" in reminder, reminder
        assert "still remembers" in plain(tr("help.clear", "en", **PALETTE))
        print("clear: empties the screen, says the conversation is kept, names the real fresh start  ✓")


asyncio.run(main())

# Every goal screen is opened in the session's language (one call used to forget it).
calls = [(f.name, n) for f in sorted((ROOT / "tui").glob("*.py"))
         for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
         if re.search(r"\bGoalScreen\(", line) and "class " not in line and "language=" not in line]
assert not calls, calls
print("goal: every GoalScreen is opened with the session language  ✓")

print("TUI FIRST HOUR OK — it moves, it tells, it stays where you type. amaze!")
