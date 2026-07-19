"""Dream pass smoke test: fake local model, real store + trajectories.

Covers: episode digestion with evidence, fact ADD and ARCHIVE reconciliation,
MEMORY.md dream-section ownership, already-dreamed detection, dry-run safety,
and lockfile cleanup. No Ollama, no API."""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

import rockycode.session as S
from rockycode.dream.core import DREAM_MARK_START, DreamRunner
from rockycode.memory import Memory, MemoryStore

# isolate the global trajectory store + registry in a temp home
_TMP_HOME = Path(tempfile.mkdtemp(prefix="rockyhome-"))
S.HOME_ROOT = _TMP_HOME / ".rockycode"
S.REGISTRY = S.HOME_ROOT / "projects.json"

WD = Path.cwd()
PID = S.get_project(WD).id

# a small but realistic trajectory in the GLOBAL store, tagged with this project
traj_dir = S.global_traj_dir()
traj_dir.mkdir(parents=True)
SID = "20260613-010101-abcd1234"
lines = [
    {"kind": "meta", "data": {"model": "fake", "project_id": PID, "source": "chat"}},
    {"kind": "message", "data": {"role": "system", "content": "you are rocky"}},
    {"kind": "message", "data": {"role": "user", "content": "fix the flaky login test"}},
    {"kind": "message", "data": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"pytest tests/login\"}"}}]}},
    {"kind": "message", "data": {"role": "tool", "tool_call_id": "c1", "content": "[exit 1]\n1 failed"}},
    {"kind": "message", "data": {"role": "assistant", "content": "fixed by mocking the clock. amaze!"}},
    {"kind": "outcome", "data": {"steps": 2}},
]
(traj_dir / f"{SID}.jsonl").write_text("\n".join(json.dumps(l) for l in lines))

DIGEST_ANSWER = """\
## task
Fix the flaky login test.
## outcome
success — test passes after mocking the clock.
## worked
- mock the clock in login tests
## failed
- rerunning the test without changes
## facts
- login tests need a mocked clock or they flake
- test suite lives under tests/login
## importance
7
"""


class FakeChat:
    def __init__(self):
        self.prompts = []

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        if "consolidating" in prompt:
            return DIGEST_ANSWER
        if "NEW FACT" in prompt:
            if "NEW FACT: login tests need a mocked clock" in prompt:
                return "ARCHIVE old-clock-fact"
            return "ADD"
        if "state section" in prompt:
            return "- focus: login test stability\n- gotcha: clock must be mocked"
        return "NOOP"


store = MemoryStore.for_workdir(WD)
# an existing memory the dream should supersede
store.save(Memory(name="old-clock-fact", type="fact",
                  description="login tests are fine with the real clock",
                  body="login tests can use the real system clock, mocked clock not needed"))


async def main():
    fake = FakeChat()
    runner = DreamRunner(WD, chat=fake)
    report = await runner.run()

    assert report.sessions_digested == 1, report
    ep = store.get(f"ep-{SID[:15]}")
    assert ep is not None and ep.evidence == [SID] and ep.importance == 7, ep
    assert "mocking the clock" in ep.body or "mock the clock" in ep.body

    # reconciliation: one fact archived the stale memory, facts were added
    assert report.facts_archived == 1, report.decisions
    assert store.get("old-clock-fact").status == "archived"
    assert report.facts_added >= 1
    added = [m for m in store.load_all() if m.type == "fact" and m.origin == "dream"]
    assert added, "no dream-added facts"

    # MEMORY.md: dream owns its marked section only
    index_text = (store.root / "MEMORY.md").read_text()
    assert DREAM_MARK_START in index_text and "login test stability" in index_text
    assert not runner._lock.exists(), "lockfile leaked"
    print("dream pass OK —", len(report.decisions), "decisions")

    # second run: session already digested via episode evidence → no-op
    runner2 = DreamRunner(WD, chat=FakeChat())
    report2 = await runner2.run()
    assert report2.sessions_digested == 0, report2
    print("already-dreamed detection OK")

    # dry-run never writes: new session, then check nothing landed
    (traj_dir / "20260613-020202-ffff0000.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines))
    n_before = len(store.load_all(include_archived=True))
    text_before = (store.root / "MEMORY.md").read_text()
    runner3 = DreamRunner(WD, chat=FakeChat(), dry_run=True)
    report3 = await runner3.run()
    assert report3.sessions_digested == 1 and report3.decisions
    assert len(store.load_all(include_archived=True)) == n_before
    assert (store.root / "MEMORY.md").read_text() == text_before
    print("dry-run safety OK")

    # stale lock is ignored, fresh lock blocks
    runner._lock.write_text("123")
    os.utime(runner._lock, (time.time() - 7200, time.time() - 7200))
    assert runner._acquire_lock(), "stale lock should be reclaimed"
    try:
        await DreamRunner(WD, chat=FakeChat()).run()
        raise AssertionError("fresh lock should block a second dream")
    except RuntimeError:
        pass
    runner._release_lock()
    print("lockfile OK")


asyncio.run(main())
print("SMOKE OK — rocky dream, bad memory go away. amaze!")
