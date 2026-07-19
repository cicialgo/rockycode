"""Goal-mode phase 2: budget caps + workspace isolation. No API."""
import subprocess
import tempfile
import time
from pathlib import Path

from rockycode.engine.budget import GoalBudget, recommended
from rockycode.engine.worktree import GoalWorkspace
from rockycode.pricing import UsageLedger

OFFPEAK = None  # add() uses real time; peak only affects cost past mid-July — fine here


# ---- budget -----------------------------------------------------------------

def test_budget():
    led = UsageLedger()
    # spend cap: nothing spent → not exceeded; after a big burst → exceeded
    b = GoalBudget(max_usd=0.001, currency="usd")
    b.start()
    assert b.exceeded(led) is None
    led.add("deepseek-v4-pro", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
    assert "spend cap" in (b.exceeded(led) or ""), b.exceeded(led)

    # token cap
    bt = GoalBudget(max_tokens=1_000)
    bt.start()
    assert bt.exceeded(UsageLedger()) is None
    led2 = UsageLedger()
    led2.add("deepseek-v4-flash", {"prompt_tokens": 800, "completion_tokens": 300})
    assert "token cap" in (bt.exceeded(led2) or ""), bt.exceeded(led2)

    # wallclock cap (0s → immediately exceeded)
    bw = GoalBudget(max_seconds=0.0)
    bw.start()
    time.sleep(0.001)
    assert "wallclock" in (bw.exceeded(UsageLedger()) or "")

    # recommended defaults + human descriptions
    r = recommended("usd")
    assert r.max_usd == 5.0 and r.max_seconds == 8 * 3600
    assert "$5.00" in r.describe() and "8h" in r.describe()
    assert "up to $5.00" in r.preflight_note()
    assert "¥35.00" in recommended("cny").describe()
    assert "NO LIMIT" in GoalBudget().describe()
    print("budget: spend/token/wallclock caps + recommended defaults + notes  ✓")


# ---- workspace isolation ----------------------------------------------------

def test_worktree():
    repo = Path(tempfile.mkdtemp(prefix="rockygoal-repo-"))

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    ws = GoalWorkspace.create(repo, "t123")
    try:
        assert ws.path.exists() and ws.path != repo
        assert ws.branch == "goal/t123"
        # edit in the copy — the REAL repo file must be untouched (isolation)
        (ws.path / "a.txt").write_text("changed by goal\n")
        assert (repo / "a.txt").read_text() == "original\n", "ISOLATION BROKEN"
        assert "changed by goal" in ws.diff()
    finally:
        ws.cleanup(keep=False)
    assert not ws.path.exists()
    print("worktree: goal edits isolated from the real repo, diff + cleanup work  ✓")


def test_worktree_copy_fallback():
    plain = Path(tempfile.mkdtemp(prefix="rockygoal-plain-"))
    (plain / "b.txt").write_text("hi\n")
    ws = GoalWorkspace.create(plain, "p1")
    try:
        assert ws.branch is None and ws.path.exists()
        (ws.path / "b.txt").write_text("changed\n")
        assert (plain / "b.txt").read_text() == "hi\n"  # isolated
    finally:
        ws.cleanup(keep=False)
    assert not ws.path.exists()
    print("worktree: non-git repo falls back to an isolated directory copy  ✓")


test_budget()
test_worktree()
test_worktree_copy_fallback()
print("GOAL PHASE-2 SMOKE OK — budget caps + workspace isolation. amaze!")
