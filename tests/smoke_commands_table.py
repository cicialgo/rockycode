"""The slash-command table: what is listed, what still runs when hidden, and
the two words that replaced /delivery. No Textual pilot — the table is plain
data and the handlers take any app-shaped object.

  1) /proposals and /routines are listed (help + completion) only while
     evolve is on or that inbox has something waiting; typed in full they run.
  2) /queue on|off sets the delivery mode; bare /queue reports it; /delivery
     is an unknown command now.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROCKYCODE_HOME", str(ROOT / ".rockycode-smoke-home"))

from rockycode.tui import commands  # noqa: E402


class FakeApp:
    """Just enough app for the handlers: a locale, a transcript, the delivery state."""

    def __init__(self, evolve: str = "off", inbox: dict | None = None) -> None:
        self._evolve = evolve
        self._inbox = inbox or {}
        self._delivery_mode = "interrupt"
        self._delivery = SimpleNamespace(pending=[], paused=False)
        self._waiting_external = False
        self.said: list[str] = []
        self.rendered = 0
        self.workers: list[str] = []

    def _t(self, key: str, **params) -> str:
        return key + (" " + " ".join(f"{k}={v}" for k, v in params.items()) if params else "")

    def _add(self, widget):  # the handlers speak through commands._say, captured below
        async def add() -> None:
            self.said.append(str(widget))
        return add()

    def _render_status(self) -> None:
        self.rendered += 1

    def _review_proposals(self) -> None:
        self.workers.append("proposals")

    def _review_routines(self) -> None:
        self.workers.append("routines")

    def open_parked(self) -> None:
        self.workers.append("parked")

    def _unsent_pickups(self) -> None:
        self.workers.append("unsent")


async def _capture(app, text: str, cls: str = "tool-line") -> None:
    app.said.append(text)

commands._say = _capture  # the text itself, not a Static widget


# ---- 1) the evolve inboxes are listed only while they earn it --------------------
off = FakeApp(evolve="off")
assert "/proposals" not in commands.names(off) and "/routines" not in commands.names(off)
assert "help.proposals" not in commands.help_text(off) and "help.routines" not in commands.help_text(off)
on = FakeApp(evolve="auto")
assert "/proposals" in commands.names(on) and "/routines" in commands.names(on)
assert "help.proposals" in commands.help_text(on) and "help.routines" in commands.help_text(on)
waiting = FakeApp(evolve="off", inbox={"proposals": 2, "routines": 0})
assert "/proposals" in commands.names(waiting) and "/routines" not in commands.names(waiting)
due = FakeApp(evolve="off", inbox={"routines": 1})
assert "/routines" in commands.names(due) and "/proposals" not in commands.names(due)
assert commands.names() == [c.name for c in commands.COMMANDS], "no app = the whole table"
# Hidden is not gone: typed in full, the inbox still opens.
asyncio.run(commands.dispatch(off, "/proposals"))
asyncio.run(commands.dispatch(off, "/routines"))
assert off.workers == ["proposals", "routines"], off.workers
# The order of the help card follows the table, minus the hidden rows.
listed = [c.name for c in commands.shown(off)]
assert listed == [c.name for c in commands.COMMANDS if c.name not in ("/proposals", "/routines", "/parked")]
print("evolve inboxes: listed while evolve is on or something waits; hidden still runs  ✓")
# /parked is the same kind of row: listed while a step yolo parked waits for the user.
assert "/parked" not in commands.names(off) and "help.parked" not in commands.help_text(off)
parked = FakeApp()
parked._approver = SimpleNamespace(parked=[object()])
assert "/parked" in commands.names(parked) and "help.parked" in commands.help_text(parked)
asyncio.run(commands.dispatch(off, "/parked"))
assert off.workers[-1] == "parked", "typed in full, it opens (and says nothing waits)"
print("/parked: listed while something waits for the user; typed, it always opens  ✓")

# ---- 2) /queue on|off replaced /delivery -------------------------------------------
app = FakeApp()
asyncio.run(commands.dispatch(app, "/queue on"))
assert app._delivery_mode == "queue" and app.rendered == 1
assert app.said[-1].startswith("app.queue_summary mode=queue count=0 state=app.queue_state_ready"), app.said[-1]
asyncio.run(commands.dispatch(app, "/queue OFF"))
assert app._delivery_mode == "interrupt" and app.rendered == 2
asyncio.run(commands.dispatch(app, "/queue"))
assert app.said[-1].startswith("app.queue_summary mode=interrupt"), app.said[-1]
asyncio.run(commands.dispatch(app, "/queue later"))
assert app.said[-1] == "app.queue_usage", app.said[-1]
asyncio.run(commands.dispatch(app, "/delivery queue"))
assert app.said[-1] == "commands.unknown p1=/delivery", app.said[-1]
assert app._delivery_mode == "interrupt"
assert commands.lookup("/delivery") is None and "/delivery" not in commands.names()
print("/queue on|off is the one delivery command; /delivery is unknown  ✓")
