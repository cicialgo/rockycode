"""Dream pass smoke test (evolve v2): fake session model, real store + trajectories.

Covers: cold-reflect of a straggler (records appended back to the trajectory,
so re-runs are free), consolidation from WARM reflect records (no cold call),
pattern fold with per-session dedupe + cross-session reinforcement, fact ADD
and ARCHIVE reconciliation, MEMORY.md dream-section ownership, already-dreamed
detection, dry-run safety, and lockfile cleanup. No Ollama (gone), no API."""
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

traj_dir = S.global_traj_dir()
traj_dir.mkdir(parents=True)
SID = "20260613-010101-abcd1234"       # straggler: no reflect records → cold
SID_WARM = "20260613-030303-eeee1111"  # warm: carries its own reflect records


def base_lines():
    return [
        {"kind": "meta", "data": {"model": "fake", "project_id": PID, "source": "chat"}},
        {"kind": "message", "data": {"role": "system", "content": "you are rocky"}},
        {"kind": "message", "data": {"role": "user", "content": "fix the flaky login test"}},
        {"kind": "message", "data": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"pytest tests/login\"}"}}]}},
        {"kind": "message", "data": {"role": "tool", "tool_call_id": "c1", "content": "[exit 1]\n1 failed"}},
        {"kind": "message", "data": {"role": "assistant", "content": "fixed by mocking the clock. amaze!"}},
        {"kind": "outcome", "data": {"source": "heuristic", "turns": 2, "tool_calls": 1}},
    ]


(traj_dir / f"{SID}.jsonl").write_text("\n".join(json.dumps(l) for l in base_lines()) + "\n")

COLD_ANSWER = json.dumps({
    "grade": {"completion": 0.9, "adherence": 0.8, "efficiency": 0.6,
              "user_feeling": 0.7, "rationale": "test fixed after one retry"},
    "episode": {"task": "Fix the flaky login test.", "outcome": "success",
                "worked": ["mock the clock in login tests"],
                "failed": ["rerunning the test without changes"]},
    "facts": ["login tests need a mocked clock or they flake",
              "test suite lives under tests/login"],
    "patterns": [{"kind": "habit", "reinforces": None, "name": "morning-arxiv-check",
                  "description": "user asks for new arxiv papers by hand", "advice": ""}],
    "skills_used": [],
    "summary": "Fixed the login flake by mocking the clock.",
})

# a warm session: TWO checkpoints both naming the same habit — per-session
# dedupe must count them as ONE observation
_WARM_PATTERN = {"kind": "habit", "reinforces": "morning-arxiv-check",
                 "name": "morning-arxiv-check",
                 "description": "user asks for new arxiv papers by hand", "advice": ""}
WARM_REFLECT_1 = {"seq": 1, "final": False, "at_messages": 4, "model": "fake",
                  "angles": {"completion": 0.8, "adherence": 0.8, "efficiency": 0.8, "user_feeling": 0.8},
                  "score": 0.8, "rationale": "going fine",
                  "episode": {"task": "summarize arxiv papers", "outcome": "partial",
                              "worked": [], "failed": []},
                  "facts": ["the user keeps an arxiv watchlist"],
                  "patterns": [_WARM_PATTERN], "skills_used": [], "summary": "mid-way"}
WARM_REFLECT_2 = {**WARM_REFLECT_1, "seq": 2, "final": True,
                  "episode": {"task": "summarize arxiv papers", "outcome": "success",
                              "worked": ["arxiv api"], "failed": []},
                  "facts": [], "summary": "done"}


def write_warm():
    lines = base_lines()
    lines.append({"kind": "reflect", "data": WARM_REFLECT_1})
    lines.append({"kind": "reflect", "data": WARM_REFLECT_2})
    lines.append({"kind": "outcome", "data": {"source": "reflect", "seq": 2, "score": 0.8,
                                              "angles": WARM_REFLECT_2["angles"], "rationale": "ok"}})
    (traj_dir / f"{SID_WARM}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")


class FakeChat:
    def __init__(self):
        self.prompts = []

    def cold_calls(self):
        return [p for p in self.prompts if "FINISHED coding-agent session" in p]

    async def chat(self, prompt, max_tokens=2048):
        self.prompts.append(prompt)
        if "FINISHED coding-agent session" in prompt:
            return COLD_ANSWER
        if "NEW FACT" in prompt:
            if "NEW FACT: login tests need a mocked clock" in prompt:
                return "ARCHIVE old-clock-fact"
            return "ADD"
        if "state section" in prompt:
            return "- focus: login test stability\n- gotcha: clock must be mocked"
        if "SKILL playbooks" in prompt or "draft ROUTINES" in prompt:
            return "null"
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

    assert report.sessions_digested == 1 and report.sessions_cold == 1, report
    assert len(fake.cold_calls()) == 1
    ep = store.get(f"ep-{SID[:15]}")
    assert ep is not None and ep.evidence == [SID] and ep.importance == 5, ep
    assert "mock the clock" in ep.body and "reflect score 0.79" in ep.body

    # the cold reflect writes BACK: reflect + graded outcome land in the file
    recs = [json.loads(l) for l in (traj_dir / f"{SID}.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "reflect" and r["data"].get("cold") for r in recs), \
        "cold reflect must append its record (re-runs must be free)"
    outs = [r["data"] for r in recs if r["kind"] == "outcome"]
    assert outs[-1]["source"] == "reflect" and outs[-1]["score"] == 0.79, outs

    # the pattern folded in as a habit memory
    habit = store.get("morning-arxiv-check")
    assert habit is not None and habit.type == "habit" and habit.evidence == [SID]
    assert habit.importance == 6 and report.patterns_added == 1, report

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
    assert report2.sessions_digested == 0 and report2.sessions_cold == 0, report2
    print("already-dreamed detection OK")

    # WARM session: no cold call, two checkpoints fold as ONE observation,
    # cross-session recurrence reinforces the existing habit
    write_warm()
    fake3 = FakeChat()
    report3 = await DreamRunner(WD, chat=fake3).run()
    assert report3.sessions_digested == 1 and report3.sessions_cold == 0, report3
    assert fake3.cold_calls() == [], "a warm session must never bill a cold reflect"
    habit = store.get("morning-arxiv-check")
    assert habit.importance == 7 and habit.evidence == [SID, SID_WARM], \
        "two checkpoints in one session must reinforce ONCE (per-session dedupe)"
    assert report3.patterns_reinforced == 1 and report3.patterns_added == 0, report3
    ep_warm = store.get(f"ep-{SID_WARM[:15]}")
    assert ep_warm is not None and "arxiv api" in ep_warm.body, \
        "warm consolidation reads the LAST checkpoint's episode"
    # facts union across checkpoints (they are deltas)
    assert any("watchlist" in d for d in report3.decisions), report3.decisions
    print("warm consolidation OK — free, deduped, reinforced")

    # dry-run never writes: new session, then check nothing landed
    (traj_dir / "20260613-020202-ffff0000.jsonl").write_text(
        "\n".join(json.dumps(l) for l in base_lines()) + "\n")
    n_before = len(store.load_all(include_archived=True))
    text_before = (store.root / "MEMORY.md").read_text()
    traj_before = (traj_dir / "20260613-020202-ffff0000.jsonl").read_text()
    runner4 = DreamRunner(WD, chat=FakeChat(), dry_run=True)
    report4 = await runner4.run()
    assert report4.sessions_digested == 1 and report4.decisions
    assert len(store.load_all(include_archived=True)) == n_before
    assert (store.root / "MEMORY.md").read_text() == text_before
    assert (traj_dir / "20260613-020202-ffff0000.jsonl").read_text() == traj_before, \
        "dry-run must not append reflect records either"
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
print("SMOKE OK — rocky dream on the big model now. bad memory go away. amaze!")


# Cold reflect re-sends a stored transcript: only the provider that already saw
# the session may see it again (multi-model rocky, 2026-10-15).
from rockycode.dream.core import _same_provider  # noqa: E402

assert _same_provider("deepseek-flash", "deepseek-v4-pro") is True
assert _same_provider(None, "deepseek-flash") is True          # legacy session, nothing to compare
assert _same_provider("deepseek-flash", "deepseek-flash") is True
assert _same_provider("deepseek-flash", "kimi-k3") is False     # another vendor: stays pending
assert _same_provider("no-such-model", "other-unknown") is False
print("dream provider guard ok")


# Failure evidence: the reflect grade is a model's opinion, the harness counters are what
# happened. A session that went badly keeps both in its episode note; a clean one adds nothing.
from rockycode.dream.patterns import failure_note  # noqa: E402

CLEAN = {"heuristic": {"source": "heuristic", "tool_errors": 0, "interrupts": 0, "tests": {"run": 2, "passed": 2}}}
ROUGH = {"heuristic": {"source": "heuristic", "tool_errors": 2, "engine_errors": 1, "interrupts": 1,
                       "tests": {"run": 7, "passed": 3}}}
assert failure_note(CLEAN, {"score": 0.9, "rationale": "fine"}) is None
assert failure_note({"heuristic": None}, {"score": 0.9}) is None  # an old session without counters
assert failure_note(ROUGH, {"score": 0.4, "rationale": "tests still\nfailing at the end"}).splitlines() == [
    "- graded 0.4: tests still failing at the end", "- 2 tool error(s)", "- 1 engine error(s)",
    "- 1 user interrupt(s) mid-work", "- test runs: 3 of 7 passed"]
# a kind grade does not hide what the harness saw
assert failure_note(ROUGH, {"score": 0.9}).splitlines()[0] == "- 2 tool error(s)"

SID_BAD = "20260613-040404-bad00001"
BAD_REFLECT = {**WARM_REFLECT_2, "seq": 1, "score": 0.4, "rationale": "tests still failing at the end",
               "episode": {"task": "make the ledger tests pass", "outcome": "failed",
                           "worked": [], "failed": ["patching the fixture"]},
               "facts": [], "patterns": []}


async def failing_session():
    lines = base_lines()[:-1]
    lines.append({"kind": "outcome", "data": {"source": "heuristic", "turns": 3, "tool_calls": 9, "tool_errors": 2,
                                              "engine_errors": 0, "interrupts": 1, "tests": {"run": 7, "passed": 3}}})
    lines.append({"kind": "reflect", "data": BAD_REFLECT})
    lines.append({"kind": "outcome", "data": {"source": "reflect", "seq": 1, "score": 0.4,
                                              "angles": BAD_REFLECT["angles"], "rationale": BAD_REFLECT["rationale"]}})
    (traj_dir / f"{SID_BAD}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    await DreamRunner(WD, chat=FakeChat()).run()
    bad = store.get(f"ep-{SID_BAD[:15]}")
    assert bad is not None and bad.importance == 6, bad
    assert bad.body.endswith("## failure signals\n- graded 0.4: tests still failing at the end\n"
                             "- 2 tool error(s)\n- 1 user interrupt(s) mid-work\n- test runs: 3 of 7 passed"), bad.body
    assert "## failed\n- patching the fixture" in bad.body  # the failed approaches stay in their own section
    assert "failure signals" not in store.get(f"ep-{SID_WARM[:15]}").body, "a clean session adds no section"


asyncio.run(failing_session())
print("dream failure evidence ok: the grade and what the harness saw, in the episode note")
