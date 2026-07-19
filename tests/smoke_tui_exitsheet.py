"""The exit feedback sheet in the TUI (headless Textual pilot, no API).

Drives the real RockyCodeApp through the exit paths (tests 1-6 force
exit_sheet="on"; 7-9 cover the off and default-"auto" gating):
  1) /exit after a real turn → the sheet pops; CLICKING a mood writes a
     `feedback` record (mood + local_only) and the app exits.
  2) /exit → clicking skip exits WITHOUT writing feedback.
  3) /exit with no turn ever run → no sheet, immediate exit, no record.
  4) keyboard path: → moves the highlight off the neutral default, Enter sends.
  5) timeout: an unanswered sheet auto-skips — /exit always actually exits.
  6) "don't ask again" persists exit_sheet=off to the global config and leaves.
  7) exit_sheet="off" → the sheet never appears, even after a real turn.
  8) default "auto" with NO live dream (no Ollama probe success) → no sheet:
     users who never dream are never asked.
  9) "auto" with the dream probe succeeded → the sheet appears.
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
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockysheet-"))

from rockycode.engine.loop import Engine
from rockycode.tui.app import ChatInput, RockyCodeApp
from rockycode.tui.exitsheet import ExitSheet


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


def chunk(content=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=None)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


async def stream(chunks):
    for c in chunks:
        yield c


class FakeCompletions:
    async def create(self, **kwargs):
        return stream([chunk(content="done."), chunk(usage=U())])


def build(**kw):
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    eng = Engine(model="fake", client=client, workdir=Path.cwd(), registry={})
    return RockyCodeApp(eng, permission="yolo", **kw), eng


def records(eng, kind):
    lines = [json.loads(l) for l in eng.trajectory.path.read_text(encoding="utf-8").splitlines()]
    return [l["data"] for l in lines if l["kind"] == kind]


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


def sheet(app):
    q = app.query(ExitSheet)
    return q.first() if len(q) else None


async def run_turn_then_exit(app, pilot):
    inp = app.query_one(ChatInput)
    inp.focus()
    inp.text = "hi rocky"
    await pilot.press("enter")
    assert await wait_until(pilot, lambda: app.engine.stats.turns == 1), "turn should finish"
    inp.text = "/exit"
    await pilot.press("enter")
    assert await wait_until(pilot, lambda: sheet(app) is not None), "sheet should pop on /exit"


async def main():
    # 1) click a mood → feedback written, app exits
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await run_turn_then_exit(app, pilot)
        await pilot.click("#sheet-mood-good")
        await wait_until(pilot, lambda: records(eng, "feedback"), timeout=3.0)
    fb = records(eng, "feedback")
    assert len(fb) == 1 and fb[0]["mood"] == "good" and fb[0]["local_only"] is True, fb
    assert fb[0]["text"] == "", fb
    print("exitsheet: clicking a mood writes the local-only feedback record  ✓")

    # 2) click skip → no feedback record, still exits
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await run_turn_then_exit(app, pilot)
        await pilot.click("#sheet-skip")
        await pilot.pause(0.2)
    assert records(eng, "feedback") == [], "skip must not write feedback"
    print("exitsheet: skip leaves silently, nothing written  ✓")

    # 3) no turn ever ran → no sheet, immediate exit (even forced on)
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/exit"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert sheet(app) is None, "no exchange → no sheet"
    assert records(eng, "feedback") == [], "no exchange → no feedback"
    print("exitsheet: open-and-close session is never nagged  ✓")

    # 4) keyboard path: → moves off the neutral default (okay) to rough, ↵ sends
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await run_turn_then_exit(app, pilot)
        await pilot.press("right")
        await pilot.press("enter")
        await wait_until(pilot, lambda: records(eng, "feedback"), timeout=3.0)
    fb = records(eng, "feedback")
    assert len(fb) == 1 and fb[0]["mood"] == "bad", fb
    print("exitsheet: arrows + enter work as paired accelerators  ✓")

    # 5) timeout: unanswered → auto-skip, the app leaves as requested
    import rockycode.tui.exitsheet as es
    es.TIMEOUT_S = 0.4
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await run_turn_then_exit(app, pilot)
        # touch nothing — the sheet's own timer must resolve it
        await wait_until(pilot, lambda: sheet(app) is None, timeout=4.0)
    assert records(eng, "feedback") == [], "timeout = skip, nothing written"
    es.TIMEOUT_S = 60.0
    print("exitsheet: unanswered sheet auto-skips — exit is never held hostage  ✓")

    # 6) "don't ask again" persists exit_sheet=off (to a patched global config)
    import rockycode.config as config
    config.GLOBAL_PATH = Path(tempfile.mkdtemp(prefix="rockycfg-")) / "config.toml"
    app, eng = build(exit_sheet="on")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        await run_turn_then_exit(app, pilot)
        await pilot.click("#sheet-never")
        await pilot.pause(0.3)
    assert records(eng, "feedback") == [], "'never' is not a rating"
    assert 'exit_sheet = "off"' in config.GLOBAL_PATH.read_text(), "config not persisted"
    print("exitsheet: don't-ask-again persists exit_sheet=off  ✓")

    # 7) exit_sheet="off" → no sheet at all, straight out
    app, eng = build(exit_sheet="off")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "hi rocky"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: app.engine.stats.turns == 1)
        inp.text = "/exit"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert sheet(app) is None, "off means off"
    assert records(eng, "feedback") == []
    print("exitsheet: exit_sheet=off suppresses the sheet entirely  ✓")

    # 8) DEFAULT "auto", no live dream: never asked (the normal-user path —
    #    no Ollama, never heard of dream → no sheet, no privacy worry)
    app, eng = build()  # default exit_sheet="auto"; no probe ran → _ollama_ok None
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "hi rocky"
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: app.engine.stats.turns == 1)
        inp.text = "/exit"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert sheet(app) is None, "auto without a live dream must never ask"
    assert records(eng, "feedback") == []
    print("exitsheet: default auto never bothers a dream-less user  ✓")

    # 9) "auto" with the dream probe succeeded → the sheet has a consumer, ask
    app, eng = build()
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.1)
        app._ollama_ok = True  # what a successful launch probe sets
        await run_turn_then_exit(app, pilot)
        await pilot.click("#sheet-mood-good")
        await wait_until(pilot, lambda: records(eng, "feedback"), timeout=3.0)
    fb = records(eng, "feedback")
    assert len(fb) == 1 and fb[0]["mood"] == "good", fb
    print("exitsheet: auto asks exactly when the dream is alive  ✓")

    print("EXITSHEET SMOKE OK — rocky asks once, remembers locally. amaze!")


asyncio.run(main())
