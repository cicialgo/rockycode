"""The /proposals review card in the TUI (headless Textual pilot, no API).

Paths driven through the real RockyCodeApp:
  1) launch with pending proposals → the muted "waiting" line appears.
  2) /proposals → a card per proposal; CLICKING install writes the global
     SKILL.md and files the proposal under approved/.
  3) the next card pops for the following proposal; Esc = later keeps it
     pending and stops the walk.
  4) /proposals with an empty inbox → one gentle line, no card.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
# FORCED fresh (not setdefault): installs skills globally — see smoke_proposals.
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.chdir(tempfile.mkdtemp(prefix="rockytuiprop-"))

from textual.widgets import Static

from rockycode.dream.proposals import ARCHIVED, PENDING, Proposal, ProposalStore, skills_home
from rockycode.engine.loop import Engine
from rockycode.session import get_project
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.proposalcard import ProposalCard

WD = Path.cwd()


class FakeCompletions:
    async def create(self, **kwargs):
        raise AssertionError("no API calls expected in this test")


def build():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=WD, registry={})
    return RockyCodeApp(eng, permission="yolo"), eng


def card(app):
    q = app.query(ProposalCard)
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
    pid = get_project(WD).id
    store = ProposalStore()
    store.save(Proposal(name="first-skill", description="the first draft",
                        project_id=pid, project_name="tuiprop",
                        reason="hot weakness: blind-edits", evidence=["rk_aaa"],
                        body="# first-skill\n\n## steps\n- step one"))
    store.save(Proposal(name="second-skill", description="the second draft",
                        project_id=pid, project_name="tuiprop", evidence=["rk_bbb"],
                        body="# second-skill\n\n## steps\n- step two"))

    app, eng = build()
    async with app.run_test(size=(95, 40)) as pilot:
        await pilot.pause(0.2)
        # 1) the launch line knows the inbox
        assert await wait_until(pilot, lambda: transcript_has(app, "2 dream proposal(s) waiting")), \
            "pending proposals should be announced at launch"

        # 2) /proposals → first card; click install (row 0)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/proposals"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: card(app) is not None), "no card popped"
        assert "first-skill" in str(card(app).border_title)
        await pilot.click("#prop-opt-0")
        assert await wait_until(pilot, lambda: (skills_home() / "first-skill" / "SKILL.md").exists()), \
            "install click must write the global SKILL.md"
        assert transcript_has(app, "installed") or await wait_until(
            pilot, lambda: transcript_has(app, "installed"))

        # 3) the walk continues → second card; Esc = later keeps it pending
        assert await wait_until(
            pilot, lambda: card(app) is not None and "second-skill" in str(card(app).border_title)), \
            "second proposal should pop next"
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: card(app) is None)
        assert transcript_has(app, "later")

    pending_left = store.list(PENDING, project_id=pid)
    assert [p.name for p in pending_left] == ["second-skill"], pending_left
    assert len(store.list("approved", project_id=pid)) == 1
    print("tui-proposals: launch line, click-install, later-keeps-pending  ✓")

    # 4) empty inbox → gentle line, no card
    store.archive(pending_left[0])
    app, eng = build()
    async with app.run_test(size=(95, 40)) as pilot:
        await pilot.pause(0.2)
        assert not transcript_has(app, "proposal(s) waiting")
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/proposals"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: transcript_has(app, "no pending proposals"))
        assert card(app) is None
    print("tui-proposals: empty inbox stays gentle  ✓")

    print("TUI-PROPOSALS SMOKE OK — the inbox asks once, installs never by itself. amaze!")


asyncio.run(main())
