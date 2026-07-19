"""Weakness mining (self-evolve phase 1, slice 3) — no Ollama, no API.

Contracts under test:
  - failure_note (layer 1) is free and code-side: a clean session contributes
    nothing; low judge score / error counters / failed bullets all register.
  - mine_weaknesses (layer 2): new patterns become `weakness` memories with
    session evidence; a recurring pattern REINFORCES (importance up, evidence
    appended) instead of duplicating.
  - DreamRunner integration: a failing session mines after digestion; a clean
    pass never issues a mining call; --dry-run reports without writing.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockyhome-"))
os.chdir(tempfile.mkdtemp(prefix="rockymine-"))

from rockycode import session as _session
from rockycode.dream.core import DreamRunner
from rockycode.dream.mining import _parse_items, failure_note, mine_weaknesses
from rockycode.memory.store import Memory, MemoryStore
from rockycode.session import get_project

WD = Path.cwd()

NEW_ITEM = {
    "pattern": "edits files without reading them first",
    "cause": "skips the read step under step pressure",
    "advice": "always read a file before editing it",
    "reinforces": None,
}


class FakeChat:
    """Answers digest / mining / state prompts; captures them for assertions."""

    def __init__(self, mining_answer="[]", failed_bullet="- none"):
        self.mining_answer = mining_answer
        self.failed_bullet = failed_bullet
        self.prompts = []

    def mining_calls(self):
        return [p for p in self.prompts if "recurring weaknesses" in p]

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        if "consolidating a coding agent" in prompt:
            return (f"## task\nfix the login flow\n## outcome\npartial\n## worked\n- none\n"
                    f"## failed\n{self.failed_bullet}\n## facts\n- none\n## importance\n6")
        if "recurring weaknesses" in prompt:
            return f"sure!\n{self.mining_answer}"
        return "- state line"


def write_traj(sid, heuristic):
    pid = get_project(WD).id
    lines = [
        {"t": 1, "kind": "meta", "data": {"source": "chat", "project_id": pid,
                                          "workdir": str(WD), "model": "fake"}},
        {"t": 2, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 3, "kind": "message", "data": {"role": "user", "content": "fix login"}},
        {"t": 4, "kind": "message", "data": {"role": "assistant", "content": "tried"}},
        {"t": 5, "kind": "outcome", "data": heuristic},
    ]
    traj = _session.global_traj_dir()
    traj.mkdir(parents=True, exist_ok=True)
    (traj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")


CLEAN = {"source": "heuristic", "turns": 2, "tool_calls": 3, "tool_errors": 0,
         "engine_errors": 0, "interrupts": 0, "tests": {"run": 1, "passed": 1}}
FAILING = {"source": "heuristic", "turns": 3, "tool_calls": 5, "tool_errors": 2,
           "engine_errors": 0, "interrupts": 1, "tests": {"run": 2, "passed": 0}}


async def main():
    # --- layer 1: free gate ---
    clean = {"heuristic": CLEAN, "outcome": CLEAN}
    assert failure_note(clean, {"task": "t", "failed": "- none"}) is None, \
        "a clean session must not feed the miner"
    failing = {"heuristic": FAILING,
               "outcome": {"source": "judge", "score": 0.35, "rationale": "wrong file edited"}}
    note = failure_note(failing, {"task": "fix login", "failed": "- edited utils.py blindly"})
    assert note is not None and "judge score 0.35" in note and "2 tool error(s)" in note
    assert "0/2 passed" in note and "edited utils.py blindly" in note and "interrupt" in note
    print("mining: failure_note gates free and captures every signal  ✓")

    # --- lenient item parse ---
    assert _parse_items("no array") == []
    assert _parse_items('[{"pattern": "p", "cause": "c"}]') == [], "missing advice → dropped"
    ok = _parse_items(f"here:\n{json.dumps([NEW_ITEM])}")
    assert len(ok) == 1 and ok[0]["pattern"] == NEW_ITEM["pattern"]
    print("mining: partial patterns are vibes, not weaknesses — dropped  ✓")

    # --- layer 2 direct: new + reinforce ---
    store = MemoryStore.for_workdir(WD)
    store.save(Memory(name="blind-edits", type="weakness",
                      description="edits before reading", importance=6,
                      origin="dream", evidence=["old-sid"], body="…"))
    runner = DreamRunner(WD, chat=FakeChat(mining_answer=json.dumps([
        NEW_ITEM,
        {"pattern": "same old blind edits", "cause": "c", "advice": "a",
         "reinforces": "blind-edits"},
    ])))
    await mine_weaknesses(runner, [("sid-1", "### t\n- 2 tool error(s)")])
    assert runner.report.weaknesses_added == 1 and runner.report.weaknesses_reinforced == 1
    new = store.get("edits-files-without-reading-them-first")
    assert new is not None and new.type == "weakness" and new.evidence == ["sid-1"]
    assert "**cause:**" in new.body and "**advice:**" in new.body
    reinforced = store.get("blind-edits")
    assert reinforced.importance == 7 and reinforced.evidence == ["old-sid", "sid-1"]
    print("mining: new pattern saved with evidence, recurrence reinforces  ✓")

    # --- runner integration: failing session → mined after digestion ---
    write_traj("20260709-000001-aaaaaaaa", FAILING)
    chat = FakeChat(
        mining_answer=json.dumps([{"pattern": "loses track of failing tests",
                                   "cause": "c", "advice": "a", "reinforces": None}]),
        failed_bullet="- reran the suite four times",
    )
    report = await DreamRunner(WD, chat=chat).run(limit=5)
    assert report.sessions_digested == 1 and report.weaknesses_added == 1, report
    assert len(chat.mining_calls()) == 1
    assert "reran the suite four times" in chat.mining_calls()[0], "digest bullets feed the miner"
    assert "[blind-edits]" in chat.mining_calls()[0], "known weaknesses shown for dedup"
    assert store.get("loses-track-of-failing-tests") is not None
    assert any("WEAKNESS" in d for d in report.decisions)
    print("mining: runner mines failing sessions after digestion  ✓")

    # --- a clean pass never calls the miner ---
    write_traj("20260709-000002-bbbbbbbb", CLEAN)
    chat2 = FakeChat()  # digest answers "- none" for failed
    report2 = await DreamRunner(WD, chat=chat2).run(limit=5)
    assert report2.sessions_digested == 1 and chat2.mining_calls() == [], \
        "no failure signals must mean no mining call"
    print("mining: clean passes stay miner-free  ✓")

    # --- dry-run: decisions, no writes ---
    write_traj("20260709-000003-cccccccc", FAILING)
    n_before = len(store.load_all(include_archived=True))
    chat3 = FakeChat(mining_answer=json.dumps([{"pattern": "brand new weakness",
                                                "cause": "c", "advice": "a",
                                                "reinforces": None}]))
    report3 = await DreamRunner(WD, chat=chat3, dry_run=True).run(limit=5)
    assert report3.weaknesses_added == 1 and any("WEAKNESS" in d for d in report3.decisions)
    assert len(store.load_all(include_archived=True)) == n_before, "dry-run wrote memories!"
    print("mining: --dry-run previews without writing  ✓")

    print("MINING SMOKE OK — rocky knows what rocky gets wrong. amaze!")


asyncio.run(main())
