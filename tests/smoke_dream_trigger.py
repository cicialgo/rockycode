"""Dream catch-up plumbing (self-evolve phase 1, first slice) — no Ollama, no API.

Three contracts:
  - condense() only reveals the exit sheet to LOCAL callers (feedback=True);
    the default keeps it out of any transcript a cloud judge might see.
  - DreamRunner(exclude=...) never digests the excluded (live) session.
  - The TUI's launch trigger stays perfectly silent when Ollama is unreachable.
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


def write_traj(sid: str, mood: str | None = None) -> Path:
    pid = get_project(WD).id
    lines = [
        {"t": 1, "kind": "meta", "data": {"source": "chat", "project_id": pid,
                                          "workdir": str(WD), "model": "fake"}},
        {"t": 2, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 3, "kind": "message", "data": {"role": "user", "content": "fix the bug"}},
        {"t": 4, "kind": "message", "data": {"role": "assistant", "content": "fixed it"}},
        {"t": 5, "kind": "outcome", "data": {"source": "heuristic", "turns": 1, "tool_errors": 0}},
    ]
    if mood:
        lines.append({"t": 6, "kind": "feedback",
                      "data": {"mood": mood, "text": "loved it", "local_only": True}})
    traj = _session.global_traj_dir()
    traj.mkdir(parents=True, exist_ok=True)
    p = traj / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


async def main():
    p_old = write_traj(SID_OLD, mood="good")
    write_traj(SID_LIVE)

    # --- condense: the exit sheet is caller-gated, not automatic ---
    s = load_session(p_old)
    assert s is not None and s["feedback"]["mood"] == "good", s
    cloud_safe = condense(s)
    assert "exit-feedback" not in cloud_safe and "loved it" not in cloud_safe, \
        "default condense must hide the sheet (a cloud judge may read this)"
    local = condense(s, feedback=True)
    assert "exit-feedback" in local and "mood=good" in local and "loved it" in local, local
    assert "[outcome]" in local, "the heuristic outcome should reach the dream digest"
    print("dream-trigger: exit sheet is local-caller-gated in condense  ✓")

    # --- exclude: the live session is never digested ---
    runner = DreamRunner(WD, exclude={SID_LIVE})
    ids = [x["session_id"] for x in runner._pending_sessions(limit=10)]
    assert SID_OLD in ids and SID_LIVE not in ids, ids
    print("dream-trigger: the live session is excluded from the catch-up  ✓")

    # --- TUI trigger: unreachable Ollama → silent no-op, chat undisturbed ---
    import rockycode.dream.core as dream_core
    dream_core.OLLAMA_URL = "http://127.0.0.1:9"  # nothing listens — probe fails fast

    from textual.widgets import Static

    from rockycode.engine.loop import Engine
    from rockycode.memory.store import MemoryStore
    from rockycode.tui.app import RockyCodeApp

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=None))
    eng = Engine(model="fake", client=client, workdir=WD, registry={})
    eng.memory_store = MemoryStore.for_workdir(WD)  # dream gate: memory must be on
    app = RockyCodeApp(eng, permission="yolo")      # dream defaults to "auto"
    async with app.run_test(size=(95, 34)) as pilot:
        await pilot.pause(0.6)  # give the probe time to fail
        assert not any("dreamed" in str(w.render()) for w in app.query(Static)), \
            "no Ollama must mean no dream line, no error, no nagging"
    print("dream-trigger: unreachable Ollama stays perfectly silent  ✓")

    print("DREAM-TRIGGER SMOKE OK — rocky dreams when it can, quietly. amaze!")


asyncio.run(main())
