"""Explore: buy verified findings from a bounded, read-only, fresh-context run.

The primitive underneath rocky's delegation features. A parent (the chat model,
or the goal verify/review path) purchases an investigation at a fixed price —
capped steps, capped wall-clock, its own context — and receives ONLY the final
report; the child's search noise (greps, file dumps, dead ends) never enters
the parent's history or its prompt-cache path.

What makes the report trustworthy is the FINDINGS CONTRACT: claims must cite
evidence as `path:line "anchor text"`, and check_citations() mechanically
re-verifies every citation against the tree (or a git ref for branch reviews)
before the parent sees the report. Hallucinated evidence is flagged inline —
a prompt rule in other harnesses, a checker here. The same footer doubles as
an automatic grounding signal on each logged episode (source: "explore").

Safety model (mechanisms, not prompt text):
- The child's registry is BUILT read-only: the safe-tier subset of the normal
  registry (read_file / grep / glob / check_code) plus a hard-gated read-only
  bash (allowlisted binaries, relative paths only, no shell metacharacters —
  see _ro_bash_check). No write_file, no edit_file.
- No `explore` tool in the child registry → children are leaf workers. Depth
  cap of 1 by construction; no recursion counter to get wrong.
- The parent-facing tool is risk="safe", which is a CONTRACT with loop.py's
  batch rule: a batch is concurrent only if every call is safe-tier. Because
  a child mutates nothing, several explore calls in one assistant turn fan
  out in parallel through the existing gather path — and any future WRITER
  role must register as "risky" so the same rule serializes it with all
  other mutations. Parallel writers stay impossible by construction.
- Children inherit the parent's workdir/allowed_roots jail, and their tool
  outputs (and the report returned to the parent) pass through the normal
  execute() redaction.

DeepSeek economics: each role's system prompt is a byte-stable constant and
the child toolset is fixed, so every explore of a role shares the same prompt
prefix — repeated purchases in a session hit the prefix cache instead of
re-paying the parent's ever-growing history.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from pathlib import Path
from typing import Optional

from rockycode.engine import tools as tools_mod
from rockycode.engine.events import TurnFinished
from rockycode.engine.tools import Tool

EXPLORE_MAX_STEPS = 20     # parent runs 50; a focused purchase should not need more
EXPLORE_FINALIZE_STEPS = 2
EXPLORE_EFFORT = "high"    # parent default is "max"; delegated lookups don't need it
EXPLORE_TIMEOUT_S = int(os.getenv("ROCKYCODE_EXPLORE_TIMEOUT", "600"))
CITATION_CAP = 30          # citations checked per report — bounds checker cost

# ---------------------------------------------------------------------------
# Role prompts — BYTE-STABLE constants (task/context go in the user message,
# never in here) so every explore of a role reuses the same cached prefix.
# ---------------------------------------------------------------------------

_OUTPUT_CONTRACT = """
Report format — your final message is ALL the buyer will see, so it must stand
alone:
- FINDINGS: what you established. Ground every claim in a citation.
- EVIDENCE: one bullet per citation, EXACTLY this shape (straight quotes):
  - path/to/file.py:123 "text copied verbatim from that line"
  ONE line number (never a range); the path written from the REPO ROOT
  (rockycode/engine/loop.py, never just loop.py); nothing between the path
  and the colon — no backticks, no "(branch)" notes. When you reviewed a
  branch or ref, cite the same plain way: the harness checks against the ref
  you were given.
  The harness re-reads every citation and flags any it cannot verify — an
  unverifiable citation is worse than none, so quote real lines only.
- GAPS: what you could not determine, and which searches you ran that came up
  empty (a negative claim without the searches behind it is worthless).
Be complete but not padded; the report replaces your whole transcript."""

EXPLORE_PROMPT = """You are rocky's explore agent: a read-only investigator \
answering one focused question about a codebase. You work in a fresh context; \
the agent that bought this investigation sees none of your tool calls — only \
your final report.

Investigate thoroughly: prefer grep/glob to locate, read_file to confirm. \
Follow the code, not your assumptions; check more than one naming convention \
before concluding something does not exist. You cannot write, edit, or \
delegate — if the task seems to need that, report it as a finding instead.
""" + _OUTPUT_CONTRACT

REVIEW_PROMPT = """You are rocky's review agent: an independent, read-only \
second opinion on a change (a diff, branch, or set of files). You work in a \
fresh context on purpose — judge only what the code says, not what the author \
intended. The buyer sees only your final report.

Read the change AND enough surrounding code to judge it in context (callers, \
tests, error paths). Rank what you find by severity; a missed failure mode \
outranks any style point, and style points are out of scope unless asked. \
Confirm the good as well: say what you checked and found sound, so silence \
is not ambiguous. Start your report with a one-line VERDICT.
""" + _OUTPUT_CONTRACT

VERIFY_PROMPT = """You are rocky's verify agent: a read-only inspector deciding \
whether a claimed milestone is ACTUALLY complete in the working tree. You work \
in a fresh context on purpose — judge the code's end state, never the worker's \
account of it.

Inspect what changed (git status, git diff, read the touched files; run \
check_code if useful) and test the claim against reality. The buyer supplies \
baseline-vs-now check output and decision rules in the task; apply them \
exactly. Your report's FIRST line must be exactly "PASS — <one-line reason>" \
or "FAIL — <one-line reason>" — the verdict first, no hedging before it.
""" + _OUTPUT_CONTRACT

# "verify" is goal-internal: reachable through make_goal_verifier, deliberately
# absent from the chat tool's role enum (EXPLORE_SCHEMA).
ROLE_PROMPTS: dict[str, str] = {
    "explore": EXPLORE_PROMPT,
    "review": REVIEW_PROMPT,
    "verify": VERIFY_PROMPT,
}

# ---------------------------------------------------------------------------
# Read-only bash gate. safety.classify_command is a DANGER classifier (an
# innocuous `echo x > f` passes it), so the child gets its own gate that is
# read-only by construction. Layered, strictest first:
#   1. no shell metacharacters at all: chaining, redirects (both ways — `<`
#      would still open arbitrary files), backgrounding, substitution, `$`
#      expansion, multi-line. Single pipes are the one composition allowed.
#   2. every pipe segment's binary must be on a small read-only allowlist;
#      git additionally needs an allowlisted read-only subcommand (this also
#      kills `git -C /elsewhere`, since `-C` is not a subcommand).
#   3. relative paths only — cwd is the jailed workdir, so every operand
#      stays in-tree without parsing which args are paths. `cat` is deliberately
#      NOT allowlisted: in-tree reads are read_file's job (jailed, line-numbered,
#      secret-refusing).
#   4. tokens matching the secret-file patterns (.env, id_rsa, *.pem …) are
#      refused, mirroring read_file. Residual risk — secrets already committed
#      to git history via `git show` — is accepted: execute() redaction still
#      scrubs known token shapes from the output.
# ---------------------------------------------------------------------------

_RO_BINS = {"ls", "wc", "head", "tail", "stat", "file", "du", "diff", "git"}
_RO_GIT_SUBS = {
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "rev-parse", "ls-files", "grep", "branch",
}
# `git branch` mutates with these; listing stays allowed.
_GIT_BRANCH_MUTATING = re.compile(r"(^|\s)(-d|-D|-m|-M|-c|-C|--delete|--move|--copy|--force)(\s|$)")
_FORBIDDEN = re.compile(r"[;&`><$\n]|--output\b")

_RO_REFUSAL = (
    "[blocked] explore bash is read-only: allowlisted binaries only "
    f"({', '.join(sorted(_RO_BINS))}; git subcommands: {', '.join(sorted(_RO_GIT_SUBS))}), "
    "relative paths, single pipes; no redirects, chaining, `$`, or secret files. "
    "Use read_file / grep / glob for file access."
)


def _ro_bash_check(command: str) -> Optional[str]:
    """Return a refusal string, or None if *command* is read-only-safe."""
    if _FORBIDDEN.search(command):
        return _RO_REFUSAL
    for segment in command.split("|"):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return _RO_REFUSAL  # unbalanced quotes — refuse rather than guess
        if not tokens:
            return _RO_REFUSAL  # empty segment (also catches `a || b` remnants)
        head, rest = tokens[0], tokens[1:]
        if head not in _RO_BINS:  # also refuses VAR=x prefixes: '=' names no bin
            return _RO_REFUSAL
        if head == "git":
            if not rest or rest[0] not in _RO_GIT_SUBS:
                return _RO_REFUSAL
            if rest[0] == "branch" and _GIT_BRANCH_MUTATING.search(segment):
                return _RO_REFUSAL
        for tok in rest:
            if tok.startswith("-"):
                continue
            if tok.startswith(("/", "~")) or ".." in tok.split("/"):
                return _RO_REFUSAL  # relative, in-tree paths only
            if tools_mod._is_secret_file(Path(tok)):
                return _RO_REFUSAL
    return None


_RO_BASH_SCHEMA = tools_mod._fn_schema(
    "bash",
    "Run a READ-ONLY shell command in the working directory (git status/log/"
    "diff/show/blame, ls, wc, head, tail, stat, du, diff; single pipes allowed). "
    "Anything that writes, chains, redirects, or leaves the tree is refused.",
    {"command": {"type": "string", "description": "The read-only command to run."}},
    ["command"],
)


def build_explore_registry(
    workdir: Path, allowed_roots: tuple[Path, ...] = ()
) -> dict[str, Tool]:
    """The child's toolset: the safe tier of the normal registry plus gated
    read-only bash. Built FRESH (not filtered from the parent's live registry)
    so session extras — MCP, memory, web, skills — never leak into children,
    and the toolset stays deterministic and byte-stable per role."""
    base = tools_mod.build_registry(workdir, allowed_roots)
    reg = {name: t for name, t in base.items() if t.risk == "safe"}

    async def _ro_bash(command: str) -> str:
        refusal = _ro_bash_check(command)
        if refusal:
            return refusal
        return await tools_mod._bash(command, workdir=workdir)

    # Read-only by construction, hence safe-tier: the child's own read batches
    # (including bash) parallelize through the same loop.py rule.
    reg["bash"] = Tool(name="bash", schema=_RO_BASH_SCHEMA, fn=_ro_bash, risk="safe")
    return reg  # note: no `explore` tool — children cannot delegate


# ---------------------------------------------------------------------------
# The citation checker — the original core of the findings contract. Parses
# `path:line "anchor"` citations out of the report and re-verifies each one
# against the working tree (or `git show <ref>:<path>` for branch reviews).
# Lenient-but-honest v1: the anchor may sit within ±3 lines of the stated
# line (models miscount); a miss marks the citation [unverified] in a footer
# rather than rejecting the report — a fuzzy child must not deadlock a review.
# ---------------------------------------------------------------------------

# Tolerates the deviations models actually produce (seen in E2E): backticked
# paths (stripped before scanning), a short parenthetical between path and
# colon ("(branch)"), and line ranges (first line taken).
_CITE_RX = re.compile(
    r'(?P<path>[A-Za-z0-9_][\w./-]*\.[A-Za-z0-9_]{1,8})'
    r'(?:\s*\([^)\n]{1,24}\))?'
    r':(?P<line>\d{1,6})(?:-\d{1,6})?'
    r'(?:\s*[—–-]*\s*"(?P<snip>[^"\n]{3,160})")?'
)
_CITE_WINDOW = 3  # anchor accepted within ± this many lines of the stated line


def _norm(s: str) -> str:
    return " ".join(s.split())


async def _git_show(workdir: Path, ref: str, path: str) -> Optional[str]:
    """Contents of *path* on *ref*, or None. Lets citations into a not-checked-
    out goal branch verify against what the branch actually says."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(workdir), "show", f"{ref}:{path}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", "replace") if proc.returncode == 0 else None
    except OSError:
        return None


def _anchor_ok(text: str, line: int, snip: Optional[str]) -> tuple[bool, str]:
    lines = text.splitlines()
    if snip is None:
        # No anchor to check — existence-only (weak) verification.
        ok = 1 <= line <= len(lines)
        return ok, "" if ok else "line beyond end of file"
    lo, hi = max(0, line - 1 - _CITE_WINDOW), min(len(lines), line + _CITE_WINDOW)
    target = _norm(snip)
    if any(target in _norm(ln) for ln in lines[lo:hi]):
        return True, ""
    # The check exists to catch FABRICATED evidence: a verbatim quote with a
    # stale line number (models miscount; seen in E2E) is sloppy, not fake.
    if any(target in _norm(ln) for ln in lines):
        return True, ""
    return False, "anchor not found in file"


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build"}


def _resolve_short_path(workdir: Path, rel: str) -> Optional[Path]:
    """A citation like 'loop.py:51' with the repo-root prefix missing (the most
    common model deviation seen in E2E): accept it iff exactly ONE tree file
    ends with the cited path — honest under-qualification, not fabrication."""
    parts = tuple(Path(rel).parts)
    hits: list[Path] = []
    try:
        for p in workdir.rglob(parts[-1]):
            if any(s in p.parts for s in _SKIP_DIRS) or not p.is_file():
                continue
            if tuple(p.parts[-len(parts):]) == parts:
                hits.append(p)
                if len(hits) > 1:
                    return None  # ambiguous — refuse to guess
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


async def check_citations(
    report: str, *, workdir: Path, git_ref: Optional[str] = None
) -> str:
    """Verify every `path:line "anchor"` citation in *report*; return the
    one-line-per-problem footer the buyer sees. Never raises."""
    seen: dict[tuple, Optional[str]] = {}
    report = report.replace("`", "")  # backticked paths still cite
    for m in _CITE_RX.finditer(report):
        key = (m["path"], int(m["line"]), m["snip"])
        if key not in seen and len(seen) < CITATION_CAP:
            seen[key] = None
    if not seen:
        return "[citations: none found — treat unevidenced claims with caution]"

    for path, line, snip in list(seen):
        p, err = tools_mod._jail(path, workdir)
        text: Optional[str] = None
        if err is None and p is not None and not p.is_file():
            alt = await asyncio.to_thread(_resolve_short_path, workdir, path)
            if alt is not None:
                p = alt
        if err is None and p is not None and p.is_file():
            try:
                text = p.read_text(errors="replace")
            except OSError:
                text = None
        if text is None or (git_ref and not _anchor_ok(text, line, snip)[0]):
            # Not verifiable against the tree — try the reviewed ref's version
            # (a citation is good if it holds on EITHER side).
            branch_text = await _git_show(workdir, git_ref, path) if git_ref else None
            if branch_text is not None:
                text = branch_text
        if err is not None:
            seen[(path, line, snip)] = "path outside the workdir"
            continue
        if text is None:
            seen[(path, line, snip)] = "file not found"
            continue
        ok, why = _anchor_ok(text, line, snip)
        seen[(path, line, snip)] = None if ok else why

    bad = {k: why for k, why in seen.items() if why}
    if not bad:
        return f"[citations: {len(seen)}/{len(seen)} verified]"
    detail = ", ".join(f"{p}:{ln} ({why})" for (p, ln, _s), why in bad.items())
    return f"[citations: {len(seen) - len(bad)}/{len(seen)} verified · unverified: {detail}]"


# ---------------------------------------------------------------------------
# Running a child + the buyer-facing surfaces.
# ---------------------------------------------------------------------------


def _final_answer(history: list[dict]) -> str:
    """Last non-empty assistant text — the child's report."""
    for msg in reversed(history):
        if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
            return msg["content"].strip()
    return ""


async def run_explore(
    *,
    task: str,
    role: str,
    context: str = "",
    model: str,
    client,
    workdir: Path,
    allowed_roots: tuple[Path, ...] = (),
    thinking: bool = True,
    effort: str = EXPLORE_EFFORT,
    max_tokens: int = 384_000,
    ledger=None,
    parent_session: str = "",
    git_ref: Optional[str] = None,
    max_steps: int = EXPLORE_MAX_STEPS,
    engine_cls=None,
) -> str:
    """Buy one investigation: spawn a fresh child Engine on *task*, verify the
    report's citations, and return report + citation footer + stats line.

    *git_ref* lets branch-review citations verify against `git show ref:path`
    (a goal branch is not checked out). The child logs its own trajectory
    (source=explore, linked to the parent session) — a clean single-task
    episode whose citation footer doubles as a grounding signal. Usage folds
    into *ledger* so /cost stays truthful. *engine_cls* is a test seam, same
    spirit as goal.Driver.
    """
    prompt = ROLE_PROMPTS.get(role)
    if prompt is None:
        roles = ", ".join(sorted(ROLE_PROMPTS))
        return f"[error] unknown explore role {role!r} — expected one of: {roles}"
    if engine_cls is None:
        from rockycode.engine.loop import Engine as engine_cls  # avoid import cycle

    child = engine_cls(
        model=model,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=max_tokens,
        workdir=workdir,
        allowed_roots=allowed_roots,
        system_prompt=prompt,
        client=client,
        registry=build_explore_registry(workdir, allowed_roots),
        max_steps=max_steps,
        finalize_steps=EXPLORE_FINALIZE_STEPS,
        trajectory_meta={
            "source": "explore",
            "role": role,
            "parent_session": parent_session,
            "task": task[:200],
        },
    )
    message = task if not context else (
        f"{task}\n\nContext from the buyer:\n{context}"
    )

    steps, usage, timed_out = 0, {}, False
    t0 = time.monotonic()

    async def _drain() -> None:
        nonlocal steps, usage
        async for ev in child.run_turn(message):
            if isinstance(ev, TurnFinished):
                steps, usage = ev.steps, ev.usage

    try:
        await asyncio.wait_for(_drain(), timeout=EXPLORE_TIMEOUT_S)
    except asyncio.TimeoutError:
        timed_out = True  # salvage whatever partial answer exists below

    if ledger is not None and usage:
        ledger.add(model, usage)

    answer = _final_answer(child.history)
    footer = ""
    if timed_out:
        answer = (
            f"[timeout] explore exceeded {EXPLORE_TIMEOUT_S}s and was stopped."
            + (f" Partial output before the cutoff:\n{answer}" if answer else "")
        )
    elif not answer:
        answer = "[error] the explore run finished without producing a report"
    else:
        footer = await check_citations(answer, workdir=workdir, git_ref=git_ref)

    stats = (
        f"[explore:{role} — {steps} steps · "
        f"{usage.get('prompt_tokens', 0):,}p + {usage.get('completion_tokens', 0):,}c tokens · "
        f"{time.monotonic() - t0:.0f}s · session {child.trajectory.session_id}]"
    )
    parts = [answer] + ([footer] if footer else []) + [stats]
    return "\n\n".join(parts)


def make_branch_reviewer(engine):
    """A reviewer callable for goal_review.build_goal_tools: buys a grounded
    review of a goal branch instead of dumping its diff into the chat context.
    Bound to the live Engine; reads its settings at CALL time."""

    async def _review(branch: str) -> str:
        task = (
            f"Review the git branch `{branch}` — the committed work of an "
            f"autonomous goal run. It is NOT checked out: read its diff with "
            f"`git diff HEAD...{branch}`, its commits with "
            f"`git log --oneline HEAD..{branch}`, and any changed file's full "
            f"branch version with `git show {branch}:path/to/file`. Read enough "
            f"of the CURRENT branch (read_file) to judge the change in context — "
            f"callers, tests, error paths. Deliver: a one-line VERDICT "
            f"(merge-ready or not, and why), issues ranked by severity, and "
            f"what you checked that is sound."
        )
        return await run_explore(
            task=task,
            role="review",
            model=engine.model,
            client=engine.client,
            workdir=engine.workdir,
            allowed_roots=engine.allowed_roots,
            thinking=engine.thinking,
            max_tokens=engine.max_tokens,
            ledger=getattr(engine, "ledger", None),
            parent_session=engine.trajectory.session_id,
            git_ref=branch,
        )

    return _review


_VERDICT_LATE = re.compile(r"(?i)judgment:\s*(pass|fail)")
# Models decorate the verdict line ("VERDICT: **PASS** — …") even when told not
# to (seen in the review E2E); strip the dressing so goal.parse_verdict's strict
# first-line check sees a bare PASS/FAIL instead of conservatively failing.
_VERDICT_DRESSING = re.compile(r"(?i)\A[\s*#]*(?:verdict\s*[:—-]\s*)?[\s*]*")


def make_goal_verifier(*, client, model, workdir: Path, ledger=None, engine_cls=None):
    """Grounded milestone verification for goal mode (EngineDriver.verify): a
    read-only verify child inspects the ACTUAL tree instead of judging from the
    worker's self-summary. Returns an async callable
    (milestone, summary, baseline, checks) -> (passed, report).

    Raises when the child yields no clean verdict (or errors/times out), so the
    caller can fall back to the summary judge — same degrade-gracefully shape
    as goal_review's reviewer. Verdict semantics mirror goal._VERIFY_SYS: a
    baseline error now gone was FIXED; an honest 'nothing to do' on a clean
    state passes; pre-existing errors fail only a milestone that owned them;
    a NEW error vs baseline is a regression and fails."""

    async def _verify(*, milestone: str, summary: str, baseline: str, checks: str):
        task = (
            f"Decide whether this milestone's OBJECTIVE is achieved in the "
            f"working tree:\n  {milestone}\n\n"
            f"The worker's claim (do NOT trust it — verify in the code):\n"
            f"{summary or '(no summary given)'}\n\n"
            f"Checks BEFORE the run began:\n{baseline or '(clean — no issues)'}\n\n"
            f"Checks NOW:\n{checks}\n\n"
            f"Inspect the tree — git status / git diff for what changed, read "
            f"the touched files — and judge the END STATE by these rules: a "
            f"baseline error that is GONE now was fixed (success, never an "
            f"inconsistency); an honest 'nothing to do' on an already-clean "
            f"state PASSES; an error present in both baseline and now is "
            f"pre-existing and fails only if fixing it was THIS milestone's "
            f"objective; a NEW error absent from the baseline is a regression "
            f"this work introduced and FAILS."
        )
        report = await run_explore(
            task=task, role="verify", model=model, client=client, workdir=workdir,
            ledger=ledger, parent_session="goal-verify", max_steps=12,
            engine_cls=engine_cls,
        )
        if report.startswith(("[error]", "[timeout]")):
            raise RuntimeError(f"grounded verify unavailable: {report.splitlines()[0]}")
        body = _VERDICT_DRESSING.sub("", report.strip(), count=1)
        first = (body.splitlines() or [""])[0].strip().upper()
        if not (first.startswith(("PASS", "FAIL")) or _VERDICT_LATE.search(body)):
            raise RuntimeError("grounded verify returned no clean PASS/FAIL verdict")
        from rockycode.engine.goal import parse_verdict  # lazy: goal never imports explore
        return parse_verdict(body)

    return _verify


EXPLORE_SCHEMA = tools_mod._fn_schema(
    "explore",
    "Buy a focused READ-ONLY investigation from an explore agent with a fresh "
    "context; you receive only its final report with harness-verified "
    "citations — its search noise never enters your history. Roles: 'explore' "
    "investigates the codebase (use instead of long grep/read chains when the "
    "question spans many files); 'review' gives an independent second opinion "
    "on a diff or design. Explore agents read/grep/glob/check_code and run "
    "read-only shell; they cannot write, edit, or delegate. Several explore "
    "calls in ONE message run in parallel. The agent sees nothing of this "
    "conversation — make the task self-contained.",
    {
        "task": {
            "type": "string",
            "description": "Self-contained instructions: the question to answer or "
            "thing to review, plus what a good report must cover.",
        },
        "role": {"type": "string", "enum": ["explore", "review"]},
        "context": {
            "type": "string",
            "description": "Optional grounding: relevant paths, prior findings, constraints.",
        },
    },
    ["task", "role"],
)


def build_explore_tool(engine) -> dict[str, Tool]:
    """The chat-facing `explore` tool, bound to a live Engine. Reads the
    engine's settings at CALL time (so a ledger attached after construction is
    still found). risk='safe' is load-bearing — see the module docstring.
    Not registered yet — lands with the chat caller (step 3)."""

    async def _explore(task: str, role: str, context: str = "") -> str:
        return await run_explore(
            task=task,
            role=role,
            context=context,
            model=engine.model,
            client=engine.client,
            workdir=engine.workdir,
            allowed_roots=engine.allowed_roots,
            thinking=engine.thinking,
            max_tokens=engine.max_tokens,
            ledger=getattr(engine, "ledger", None),
            parent_session=engine.trajectory.session_id,
        )

    return {"explore": Tool(name="explore", schema=EXPLORE_SCHEMA, fn=_explore, risk="safe")}
