"""Dream catch-up plumbing (evolve v2) — no API.

Three contracts:
  - load_session collects reflect records; legacy `feedback` lines (retired
    exit sheet) are ignored, never crash, and never reach a prompt.
  - DreamRunner(exclude=...) never consolidates the excluded (live) session.
  - The TUI's launch trigger stays perfectly silent when evolve is off or
    memory is off — no dream line, no error, no nagging.
"""
import asyncio
import json
import os
import tempfile
import types
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockydreamtrig-"))

from rockycode import session as _session
from rockycode.dream.core import DreamRunner, condense, load_session
from rockycode.session import get_project

WD = Path.cwd()
SID_OLD = "20260708-000001-aaaaaaaa"
SID_LIVE = "20260708-000002-bbbbbbbb"


def write_traj(sid: str, legacy_feedback: bool = False) -> Path:
    pid = get_project(WD).id
    lines = [
        {"t": 1, "kind": "meta", "data": {"source": "chat", "project_id": pid,
                                          "workdir": str(WD), "model": "fake"}},
        {"t": 2, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 3, "kind": "message", "data": {"role": "user", "content": "fix the bug"}},
        {"t": 4, "kind": "message", "data": {"role": "assistant", "content": "fixed it"}},
        {"t": 5, "kind": "outcome", "data": {"source": "heuristic", "turns": 1, "tool_errors": 0}},
        {"t": 6, "kind": "reflect", "data": {"seq": 1, "final": True, "score": 0.9,
                                             "angles": {}, "episode": {"task": "fix the bug"},
                                             "facts": [], "patterns": [], "summary": "ok"}},
    ]
    if legacy_feedback:
        # a pre-v2 trajectory: the retired exit sheet's record must be inert
        lines.append({"t": 7, "kind": "feedback",
                      "data": {"mood": "good", "text": "loved it", "local_only": True}})
    traj = _session.global_traj_dir()
    traj.mkdir(parents=True, exist_ok=True)
    p = traj / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


async def main():
    p_old = write_traj(SID_OLD, legacy_feedback=True)
    write_traj(SID_LIVE)

    # --- reflect records load; legacy feedback lines are inert ---
    s = load_session(p_old)
    assert s is not None and len(s["reflects"]) == 1 and s["reflects"][0]["score"] == 0.9, s
    assert "feedback" not in s, "the retired sheet must not resurface on the session dict"
    text = condense(s)
    assert "loved it" not in text and "mood" not in text, \
        "legacy sheet text must never enter any prompt"
    assert "[outcome]" in text, "the outcome line should reach the cold-reflect prompt"
    print("dream-trigger: reflects load, legacy feedback lines stay inert  ✓")

    # --- exclude: the live session is never consolidated ---
    runner = DreamRunner(WD, chat=types.SimpleNamespace(), exclude={SID_LIVE})
    ids = [x["session_id"] for x in runner._pending_sessions(limit=10)]
    assert SID_OLD in ids and SID_LIVE not in ids, ids
    print("dream-trigger: the live session is excluded from the catch-up  ✓")

    # --- TUI trigger: evolve off / memory off → silent no-op ---
    from textual.widgets import Static

    from rockycode.engine.loop import Engine
    from rockycode.memory.store import MemoryStore
    from rockycode.tui.app import RockyCodeApp

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=None))

    eng = Engine(model="fake", client=client, workdir=WD, registry={})
    eng.memory_store = MemoryStore.for_workdir(WD)
    app = RockyCodeApp(eng, permission="yolo", evolve="off")
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.4)
        assert not any("dreamed" in str(w.render()) for w in app.query(Static)), \
            "evolve=off must mean no dream line, no error, no nagging"
    print("dream-trigger: evolve=off stays perfectly silent  ✓")

    eng2 = Engine(model="fake", client=client, workdir=WD, registry={})
    # no memory_store attribute → the catch-up gate must bail before anything
    app2 = RockyCodeApp(eng2, permission="yolo")  # evolve defaults to "auto"
    async with app2.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.4)
        assert not any("dreamed" in str(w.render()) for w in app2.query(Static)), \
            "memory off must mean no dream, silently"
    print("dream-trigger: memory-off stays perfectly silent  ✓")

    print("DREAM-TRIGGER SMOKE OK — rocky dreams when it can, quietly. amaze!")


asyncio.run(main())
