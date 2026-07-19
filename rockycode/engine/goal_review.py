"""Safe chat tools for reviewing and merging a goal branch.

A goal run leaves its work COMMITTED on a `goal/<slug>` branch in a separate git
worktree — never in the user's current files. Back in chat the model may be asked
to "review it" or "merge it". Rather than trust it to hand-roll git (where a bad
merge could leave the repo half-conflicted), these tools bake in the guards:

  list_goal_branches   — discover what's there (read-only)
  review_goal_branch   — the branch's diff vs where it forked (read-only)
  merge_goal_branch    — merge into the current branch, but ONLY if the tree is
                         clean, and ABORT (restoring the repo exactly) on conflict

Every tool refuses any ref that isn't a `goal/...` branch, so the model can't be
talked into merging an arbitrary branch. merge is risk="risky" → it still goes
through the normal approval gate in ask/careful mode.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rockycode.engine.tools import Tool, _fn_schema

_GOAL_BRANCH_RX = re.compile(r"^goal/[\w./-]+$")
_MAX_DIFF_CHARS = 40_000


async def _git(repo: str, *args: str) -> tuple[str, int]:
    """Run `git -C repo <args>` off the event loop → (combined output, rc)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace"), proc.returncode


def _is_goal_branch(name: str) -> bool:
    return bool(_GOAL_BRANCH_RX.match(name or ""))


async def _exists(repo: str, branch: str) -> bool:
    _out, rc = await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return rc == 0


def build_goal_tools(*, workdir: Path, reviewer=None) -> dict[str, Tool]:
    """Return {list_goal_branches, review_goal_branch, merge_goal_branch} bound to
    the host repo at *workdir*.

    *reviewer* (optional): an async callable (branch) -> report. When provided,
    review_goal_branch delegates to it — a read-only explore child reads the
    branch and returns cited, harness-verified findings — so the chat context
    receives a REVIEW instead of a 40k-char raw diff. When None (tests, callers
    that don't carry an engine), the plain truncated diff dump remains."""
    repo = str(Path(workdir).resolve())

    async def list_goal_branches() -> str:
        """List the goal/* branches in this repo, newest first, with how many
        commits each is ahead of the current branch."""
        out, rc = await _git(
            repo, "for-each-ref", "--sort=-committerdate",
            "--format=%(refname:short)\t%(contents:subject)", "refs/heads/goal")
        if rc != 0:
            return f"[error] {out.strip() or 'could not list branches'}"
        rows = [ln for ln in out.splitlines() if ln.strip()]
        if not rows:
            return "[ok] no goal branches — run /goal to create one."
        lines = ["goal branches (newest first):"]
        for row in rows:
            branch, _, subject = row.partition("\t")
            ahead, _ = await _git(repo, "rev-list", "--count", f"HEAD..{branch}")
            lines.append(f"  {branch}  (+{ahead.strip() or '?'} commits)  {subject.strip()}")
        return "\n".join(lines)

    def _diff_dump(branch: str, out: str) -> str:
        if len(out) > _MAX_DIFF_CHARS:
            head = out[:_MAX_DIFF_CHARS]
            return (f"[ok] diff of {branch} (truncated to {_MAX_DIFF_CHARS:,} chars — "
                    f"full: git -C {repo} diff HEAD...{branch}):\n{head}\n…[truncated]")
        return f"[ok] diff of {branch} vs the current branch:\n{out}"

    async def review_goal_branch(branch: str, raw: bool = False) -> str:
        """Review a goal branch. With a reviewer wired: delegate to a read-only
        explore child (full diff + surrounding code, cited findings) and return
        only its report. raw=True — or no reviewer — returns the plain
        (truncated) diff instead. Use before merge_goal_branch."""
        if not _is_goal_branch(branch):
            return f"[refused] '{branch}' is not a goal branch (must look like goal/…)."
        if not await _exists(repo, branch):
            return f"[not found] no branch '{branch}' — call list_goal_branches to see what exists."
        out, rc = await _git(repo, "diff", f"HEAD...{branch}")
        if rc != 0:
            return f"[error] {out.strip() or 'diff failed'}"
        if not out.strip():
            return f"[ok] '{branch}' has no changes vs the current branch."
        if reviewer is None or raw:
            return _diff_dump(branch, out)
        try:
            report = await reviewer(branch)
        except Exception as e:  # noqa: BLE001 — a failed review degrades to the dump
            note = f"[note] delegated review failed ({type(e).__name__}) — raw diff instead.\n"
            return note + _diff_dump(branch, out)
        return (f"[ok] delegated review of {branch} (the full diff stayed out of "
                f"this context; raw=true for the diff itself):\n{report}")

    async def merge_goal_branch(branch: str) -> str:
        """Merge a goal branch into the CURRENT branch. Guarded: refuses if the
        working tree has uncommitted changes, and aborts (restoring the repo
        exactly) if the merge conflicts — so it can never leave a half-merged mess.
        Only operates on goal/… branches."""
        if not _is_goal_branch(branch):
            return f"[refused] '{branch}' is not a goal branch (must look like goal/…). Nothing merged."
        if not await _exists(repo, branch):
            return f"[not found] no branch '{branch}' — call list_goal_branches to see what exists."

        status, _ = await _git(repo, "status", "--porcelain")
        if status.strip():
            return ("[refused] your working tree has uncommitted changes — commit or stash them "
                    "first, then merge again. Nothing was merged.")
        target, _ = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        target = target.strip() or "the current branch"

        out, rc = await _git(repo, "merge", "--no-ff", branch, "-m", f"Merge goal branch {branch}")
        if rc == 0:
            return (f"[merged] {branch} → {target}. Your files now have the changes. "
                    f"Tidy the leftover goal worktrees any time with: rockycode goal --clean "
                    f"(keeps the branches).")
        await _git(repo, "merge", "--abort")   # restore the repo byte-for-byte
        return (f"[conflict] merging {branch} into {target} hit conflicts — I aborted it, so your "
                f"repo is unchanged. Resolve manually if you want: git -C {repo} merge {branch}\n"
                f"conflicting files:\n{out.strip()[:1500]}")

    return {
        "list_goal_branches": Tool(
            name="list_goal_branches",
            schema=_fn_schema("list_goal_branches",
                              "List the goal/* branches produced by past /goal runs, with how many "
                              "commits each is ahead. Read-only.", {}, []),
            fn=list_goal_branches, risk="safe"),
        "review_goal_branch": Tool(
            name="review_goal_branch",
            schema=_fn_schema("review_goal_branch",
                              "Review a goal branch before merging. Read-only. Returns an independent "
                              "reviewer's report with verified citations (the diff itself stays out of "
                              "this context); pass raw=true if you need the plain truncated diff.",
                              {"branch": {"type": "string", "description": "The goal branch, e.g. goal/20260706-2113."},
                               "raw": {"type": "boolean", "description": "Return the raw (truncated) diff instead of the delegated review."}},
                              ["branch"]),
            fn=review_goal_branch, risk="safe"),
        "merge_goal_branch": Tool(
            name="merge_goal_branch",
            schema=_fn_schema("merge_goal_branch",
                              "Safely merge a goal branch into the current branch. Refuses a dirty tree "
                              "and aborts cleanly on conflict — never leaves a half-merged repo. Only "
                              "goal/… branches.",
                              {"branch": {"type": "string", "description": "The goal branch to merge, e.g. goal/20260706-2113."}},
                              ["branch"]),
            fn=merge_goal_branch, risk="risky"),
    }
