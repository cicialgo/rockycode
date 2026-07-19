"""The /routines due card in the TUI (headless Textual pilot, no API, no Docker).

Paths driven through the real RockyCodeApp:
  1) launch with a due (unleased) routine → the "⏰ due" line appears, and
     nothing auto-runs (no lease = no unattended spend).
  2) /routines → the card; clicking "auto" grants the 7-day lease (persisted)
     — the run itself is exercised in smoke_routine_run, not here.
  3) card for the next routine follows; Esc = later stops the walk.
  4) "off" persists enabled=false; /routines with nothing due stays gentle.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockytuirt-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.routines import Routine, RoutineStore
from rockycode.session import get_project
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.routinecard import RoutineCard

WD = Path.cwd()


class FakeCompletions:
    async def create(self, **kwargs):
        raise AssertionError("no API calls expected in this test")


def build():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=WD, registry={})
    return RockyCodeApp(eng, permission="yolo"), eng


def card(app):
    q = app.query(RoutineCard)
    return q.first() if len(q) else None


def transcript_has(app, needle):
    return any(needle in str(w.render()) for w in app.query(Static))


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def main():
    # The card's run/auto paths spawn the REAL runner (sandboxed exec → Docker).
    # Stub it here — the runner has its own smoke (smoke_routine_run); this
    # test owns the card + lease UX only. The stub never ticks the odometer,
    # so routines stay "due" across scenarios by design.
    import rockycode.routines as RT

    async def _fake_run(store, r, **kw):
        return {"status": "done", "cost": 0.0, "summary": "stub",
                "session": "rk_stub", "blocked_on": None}

    RT.run_routine = _fake_run

    pid = get_project(WD).id
    store = RoutineStore()
    store.save(Routine(name="arxiv-digest", description="daily arxiv sweep",
                       cadence="daily", prompt="sweep", workdir=str(WD),
                       project_id=pid, network=True, budget_lease=0.50))
    store.save(Routine(name="deps-bump", description="weekly deps check",
                       cadence="weekly", prompt="bump", workdir=str(WD),
                       project_id=pid))

    # 1) due line, and no auto-run without a lease
    app, eng = build()
    async with app.run_test(size=(95, 40)) as pilot:
        await pilot.pause(0.2)
        assert await wait_until(pilot, lambda: transcript_has(app, "routine(s) due")), \
            "due routines should be announced at launch"
        assert not transcript_has(app, "running (sandboxed)"), \
            "no lease must mean no unattended run"

        # 2) /routines → first card; click "auto" (row 1) → lease persisted
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/routines"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card(app) is not None), "no card popped"
        assert "arxiv-digest" in str(card(app).border_title)
        await pilot.click("#rt-opt-1")
        assert await wait_until(pilot, lambda: transcript_has(app, "auto lease granted"))
        r = store.load("arxiv-digest")
        assert r.auto and store.lease_active(r), "the lease must persist"

        # 3) next card (deps-bump); Esc = later stops the walk
        assert await wait_until(
            pilot, lambda: card(app) is not None and "deps-bump" in str(card(app).border_title))
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: card(app) is None)
        assert transcript_has(app, "later")
    print("tui-routines: due line, lease click persists, later stops the walk  ✓")

    # 4) "off" persists; nothing-due stays gentle
    app, eng = build()
    async with app.run_test(size=(95, 40)) as pilot:
        await pilot.pause(0.2)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/routines"
        await pilot.press("enter")
        # walk: arxiv-digest card first (leased but still due until it RUNS —
        # the fake engine can't run it here) → turn it off; then deps-bump → off.
        for expected in ("arxiv-digest", "deps-bump"):
            assert await wait_until(
                pilot, lambda: card(app) is not None and expected in str(card(app).border_title)), expected
            await pilot.click("#rt-opt-3")
            await pilot.pause(0.2)
        assert store.load("arxiv-digest").enabled is False
        inp.text = "/routines"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: transcript_has(app, "no routines due"))
        assert card(app) is None
    print("tui-routines: off persists, empty stays gentle  ✓")

    print("TUI-ROUTINES SMOKE OK — due once, run on your say-so. amaze!")


asyncio.run(main())
