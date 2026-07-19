"""Goal review/merge chat tools — real git, no Docker/LLM.

merge_goal_branch mutates the user's REAL repo, so its guards are the whole
point. Against throwaway repos this checks:
  - list_goal_branches   → sees the goal/* branch
  - review_goal_branch    → shows the diff; refuses non-goal refs; not-found
  - merge_goal_branch      → clean merge lands it; dirty tree refused; conflict
                             aborts (repo byte-for-byte unchanged); non-goal ref
                             and missing branch refused
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from rockycode.engine.goal_review import build_goal_tools


def sh(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def new_repo():
    d = Path(tempfile.mkdtemp())
    sh(d, "init", "-q")
    sh(d, "config", "user.email", "t@t")
    sh(d, "config", "user.name", "t")
    (d / "README.md").write_text("hello\n")
    sh(d, "add", "-A")
    sh(d, "commit", "-q", "-m", "init")
    return d.resolve()


def default_branch(repo):
    return sh(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def goal_branch(repo, name, edits):
    """Create goal/<name> with `edits` (dict path->content) committed on it, then
    return to the default branch."""
    base = default_branch(repo)
    sh(repo, "checkout", "-q", "-b", f"goal/{name}")
    for p, c in edits.items():
        (repo / p).write_text(c)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", f"goal work {name}")
    sh(repo, "checkout", "-q", base)
    return f"goal/{name}"


async def main():
    # --- list + review ---
    repo = new_repo()
    br = goal_branch(repo, "alpha", {"NEW.md": "added by goal\n"})
    tools = build_goal_tools(workdir=repo)
    listed = await tools["list_goal_branches"].fn()
    assert br in listed and "+1 commits" in listed, listed
    diff = await tools["review_goal_branch"].fn(branch=br)
    assert diff.startswith("[ok]") and "NEW.md" in diff, diff
    # refuse a non-goal ref, and a missing branch
    assert (await tools["review_goal_branch"].fn(branch=default_branch(repo))).startswith("[refused]")
    assert (await tools["merge_goal_branch"].fn(branch="main-ish")).startswith("[refused]")
    assert (await tools["merge_goal_branch"].fn(branch="goal/nope")).startswith("[not found]")
    print("list + review + refusals  ✓")

    # --- clean merge lands the file on the current branch ---
    out = await tools["merge_goal_branch"].fn(branch=br)
    assert out.startswith("[merged]"), out
    assert (repo / "NEW.md").exists(), "clean merge must bring the file in"
    print("clean merge lands the work  ✓")

    # --- dirty tree → refused, nothing changes ---
    repo2 = new_repo()
    br2 = goal_branch(repo2, "beta", {"X.md": "x\n"})
    (repo2 / "README.md").write_text("uncommitted local edit\n")   # dirty
    t2 = build_goal_tools(workdir=repo2)
    out2 = await t2["merge_goal_branch"].fn(branch=br2)
    assert out2.startswith("[refused]") and "uncommitted" in out2, out2
    assert not (repo2 / "X.md").exists(), "must not merge into a dirty tree"
    print("dirty tree refused  ✓")

    # --- conflict → aborted, repo left exactly as it was ---
    repo3 = new_repo()
    br3 = goal_branch(repo3, "gamma", {"README.md": "goal version\n"})
    (repo3 / "README.md").write_text("main version\n")             # conflicting edit on the base
    sh(repo3, "commit", "-aqm", "main edit")
    t3 = build_goal_tools(workdir=repo3)
    out3 = await t3["merge_goal_branch"].fn(branch=br3)
    assert out3.startswith("[conflict]"), out3
    assert sh(repo3, "status", "--porcelain").stdout.strip() == "", "abort must leave the repo clean"
    assert (repo3 / "README.md").read_text().strip() == "main version", "content must be untouched"
    print("conflict aborted, repo clean  ✓")

    print("GOAL REVIEW SMOKE OK — list / review / guarded merge (clean / dirty / conflict / refusals). amaze!")


asyncio.run(main())
