"""Goal orchestrator (GoalRunner): plan → work → verify → review/replan →
finalize, with budget stop, stall handling, and pre-flight safety. Fake driver,
no API — tests the loop logic deterministically."""
import asyncio
import subprocess
import tempfile
from pathlib import Path

from rockycode.engine.budget import GoalBudget
from rockycode.engine.goal import GoalRunner
from rockycode.engine.worktree import GoalWorkspace
from rockycode.pricing import UsageLedger


def _git_ws(slug):
    repo = Path(tempfile.mkdtemp(prefix="rockyorch-"))

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "x.txt").write_text("v0\n")
    git("add", ".")
    git("commit", "-q", "-m", "i")
    return GoalWorkspace.create(repo, slug)


class FakeDriver:
    def __init__(self, plan, verify_seq=None, review=None):
        self._plan = plan
        self._verify = list(verify_seq or [])
        self._review = review
        self.work_calls = 0
        self.baseline_calls = 0
        self.plan_calls = 0

    async def plan(self, objective):
        self.plan_calls += 1
        return list(self._plan), ""   # (milestones, requires)

    async def work(self, milestone, context):
        self.work_calls += 1
        return f"did {milestone}"

    async def capture_baseline(self):
        self.baseline_calls += 1

    async def verify(self, milestone, result):
        return self._verify.pop(0) if self._verify else (True, "ok")

    async def review(self, objective, remaining, done, note):
        return self._review(objective, remaining, done, note) if self._review else None


async def main():
    # 1) happy path: 2 milestones, all verify ok → done 2/2; baseline captured once
    ws = _git_ws("s1")
    drv = FakeDriver(["m1", "m2"])
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger()).run()
    ws.cleanup(keep=False)
    assert r.status == "done" and r.milestones_done == 2 and r.milestones_total == 2, r
    assert drv.baseline_calls == 1, f"baseline must be captured exactly once, got {drv.baseline_calls}"
    print("happy path → done 2/2, baseline captured once  ✓")

    # 2) budget cap already exceeded → graceful stop, no milestones done
    ws = _git_ws("s2")
    led = UsageLedger()
    led.add("deepseek-v4-flash", {"prompt_tokens": 10, "completion_tokens": 10})
    r = await GoalRunner("obj", FakeDriver(["m1", "m2"]), GoalBudget(max_tokens=1), ws, led).run()
    ws.cleanup(keep=False)
    assert r.status == "budget", r
    print("budget cap → graceful stop  ✓")

    # 3) stall → reviewer replans → continues to done
    ws = _git_ws("s3")
    seen = {"n": 0}

    def review(objective, remaining, done, note):
        seen["n"] += 1
        return ["m1b"]
    drv = FakeDriver(["m1"], verify_seq=[(False, "no"), (False, "no"), (True, "ok")], review=review)
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger(), max_stalls=2).run()
    ws.cleanup(keep=False)
    assert r.status == "done" and seen["n"] >= 1, (r, seen)
    print("stall → reviewer replans → done  ✓")

    # 4) stall → reviewer gives up (None) → stalled
    ws = _git_ws("s4")
    drv = FakeDriver(["m1"], verify_seq=[(False, "x"), (False, "x")], review=lambda *a: None)
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger(), max_stalls=2).run()
    ws.cleanup(keep=False)
    assert r.status == "stalled", r
    print("stall → no replan → stalled  ✓")

    # 5) pre-flight: a plan naming a blocked action aborts BEFORE any work runs
    ws = _git_ws("s5")
    drv = FakeDriver(["read the code", "rm -rf / to clean up", "fix"])
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger()).run()
    ws.cleanup(keep=False)
    assert r.status == "aborted" and drv.work_calls == 0, (r, drv.work_calls)
    print("pre-flight blocked action → aborted before any work  ✓")

    # 6) ask-tier action + no approver → aborted (fail-safe)
    ws = _git_ws("s6")
    drv = FakeDriver(["git push origin main"])
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger()).run()
    ws.cleanup(keep=False)
    assert r.status == "aborted" and "approval" in r.reason, r
    print("ask-tier without approval → aborted  ✓")

    # 7) commit checkpoints work onto the GOAL branch, never the user's branch
    ws = _git_ws("commit")
    origin_before = subprocess.run(["git", "-C", str(ws.origin), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
    (ws.path / "new.txt").write_text("hi")
    assert ws.commit("goal: add new.txt") is True
    assert ws.commit("goal: nothing changed") is False, "empty commit should be skipped"
    # the goal branch advanced...
    log = subprocess.run(["git", "-C", str(ws.origin), "log", "--oneline", f"{ws.base}..{ws.branch}"],
                         capture_output=True, text=True).stdout
    assert "add new.txt" in log, log
    # ...but the user's own branch did NOT move — the isolation guarantee.
    origin_after = subprocess.run(["git", "-C", str(ws.origin), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
    assert origin_after == origin_before, "user's branch moved — isolation broken!"
    assert "new.txt" in ws.diff(), ws.diff()
    ws.cleanup(keep=False)
    print("commit → checkpoints goal branch, user branch untouched, diff shows it  ✓")

    # 8) preplanned: runner skips planning + preflight, runs the given plan
    ws = _git_ws("preplanned")
    drv = FakeDriver(["MUST-NOT-RUN"])  # its plan() must never be consulted
    r = await GoalRunner("obj", drv, GoalBudget(), ws, UsageLedger(),
                         preplanned=["m1", "m2"]).run()
    ws.cleanup(keep=False)
    assert r.status == "done" and r.milestones_done == 2, r
    assert drv.plan_calls == 0, "preplanned path must not call driver.plan()"
    assert drv.baseline_calls == 1 and drv.work_calls == 2, (drv.baseline_calls, drv.work_calls)
    print("preplanned → runs the given plan, skips planning/preflight  ✓")

    print("GOAL ORCHESTRATOR SMOKE OK — plan/verify/review/budget/safety/commit/preplanned. amaze!")


asyncio.run(main())
