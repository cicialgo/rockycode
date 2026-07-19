"""Plan-mode policy: the read-only gate for interactive planning.

Plan mode is HOST state on the Engine (never a model-invoked tool — an
out-of-distribution mode tool invokes unreliably on DeepSeek, and schema churn
breaks the prefix cache; see docs/plan-mode-design.md). While it is on, every
tool call runs through gate() BEFORE the normal permission/approver flow:

  pass  — hand the call to the normal flow (permission.decide + modal):
          every 'safe'-tier read, read-only-classified bash (which still ASKS,
          never auto-allows — a read-only `cat ~/.ssh/id_rsa` must face a
          human), and web_fetch (a read of the world, SSRF-guarded, still asks).
  allow — run without asking: a write/edit whose RESOLVED target is exactly
          the session's plan file. The one writable path in the mode.
  deny  — everything else that mutates. The message teaches the model where
          to go instead (keep exploring, write the plan) rather than just
          refusing, so the turn keeps moving.

This gate decides deny-vs-ask; it is NOT a security boundary (human approval,
the file-tool jail, and the secret-file refusals are). The bash classifier is
therefore a conservative whitelist, not a bulletproof shell parser: a sneaky
"read-only" command that slips through still lands in front of the user.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Verdict:
    action: str   # "pass" | "allow" | "deny"
    message: str  # model-facing text for deny; "" otherwise


PASS = Verdict("pass", "")

# Risky-tier tools that only READ the world — forwarded to the normal ask flow.
# (web_search/web_research are already 'safe'-tier and pass on risk alone.)
_WORLD_READS = frozenset({"web_fetch"})

_WRITE_MSG = (
    "[blocked] plan mode is read-only — no code changes yet. The one writable "
    "file is the plan: {plan}. Keep exploring with read-only tools and write "
    "your plan there; the user approves it before any changes are made."
)
_BASH_MSG = (
    "[blocked] plan mode is read-only and this command could change state. "
    "Read-only commands (git log/diff/show/status/blame, ls, cat, rg, find …) "
    "may run with approval — no redirects, chaining, or substitution. Write "
    "your plan to {plan} when ready; the user approves it before any changes."
)
_TOOL_MSG = (
    "[blocked] '{tool}' is not available in plan mode — it can change state. "
    "Explore with read-only tools, then write your plan to {plan}; the user "
    "approves it before any changes are made."
)


# The plan-mode instruction rides the USER turn — never the system prompt or
# the tool list — so toggling the mode leaves the cached prompt prefix
# byte-identical (DeepSeek prefix caching is full-prefix-match only).
_MARKER = (
    "[Plan mode — planning only, no code changes. Explore with read-only tools "
    "(read-only bash and web fetches still ask for approval). BRAINSTORM first: "
    "if a decision that is genuinely the user's — scope, a tech choice, an "
    "ambiguous requirement — would shape the plan, ask the single most "
    "important question and stop; one question per turn; do NOT create the "
    "plan file while brainstorming. If the user says to just write the plan, "
    "draft immediately. DRAFT when answers stop changing the design: write the "
    "plan to {path} — a few phases, each with concrete, verifiable steps — "
    "then stop. That file is the only thing you may write; the user approves "
    "the plan before any changes are made.]"
)


def marker(plan_file: Path) -> str:
    """The per-turn plan-mode instruction (prepended to each user message)."""
    return _MARKER.format(path=plan_file)


# ── plan-file parsing ─────────────────────────────────────────────────────────
# The marker stays format-LIGHT on purpose: dictating markdown shape costs
# tokens every turn and different models write plans differently. Instead the
# parser is liberal — a phase line is a markdown heading ('## Verify') OR a
# top-level numbered item ('2. Verify', bold or plain); its bullets / indented
# numbered lines are the phase's steps. This is what the future plan→goal
# handoff reads (goal.parse_plan drops heading lines, so it can't be reused).

_PHASE_RX = re.compile(r"^(?:#{1,6}\s+|\*{0,2}\d+[.)]\*{0,2}\s+)\s*(.+?)\s*$")
_STEP_RX = re.compile(r"^(?:\s+(?:[-*•]|\d+[.)])|[-*•])\s+(.+?)\s*$")


def parse_plan_file(text: str) -> list[tuple[str, list[str]]]:
    """(phase title, [steps]) pairs from a drafted plan, shape-agnostic.

    A document title (an H1 with no steps of its own, like '# Fix add()')
    is dropped when real phases carry the steps: any step-less phase is
    pruned as long as at least one phase has steps."""
    phases: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        s = _STEP_RX.match(line)
        if s and phases:
            phases[-1][1].append(s.group(1).strip())
            continue
        p = _PHASE_RX.match(line)
        if p:
            title = p.group(1).strip().strip("*").rstrip(":：").strip()
            if title:
                phases.append((title, []))
    if any(steps for _, steps in phases):
        phases = [(t, st) for t, st in phases if st]
    return phases


def create_plan_file(workdir: Path, topic: str = "") -> Path:
    """Create (and return) a fresh plan file under .rockycode/plans/.

    The directory self-gitignores (a `*` .gitignore inside it) so drafts never
    pollute the project's git status — delete that file to start committing
    plans. Name: <date>-<topic-slug>.md, falling back to the time when no
    topic was given; an existing non-empty file of the same name gets a -N
    suffix instead of being reused (the turn-end gate watches THIS session's
    file for changes, so it must start empty)."""
    plans = workdir / ".rockycode" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    gitignore = plans / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or time.strftime("%H%M")
    base = f"{time.strftime('%Y-%m-%d')}-{slug}"
    path, n = plans / f"{base}.md", 1
    while path.exists() and path.stat().st_size > 0:
        n += 1
        path = plans / f"{base}-{n}.md"
    path.touch()
    return path


def gate(tool: str, args: dict, risk: str, plan_file: Path, workdir: Path) -> Verdict:
    """Classify one tool call under plan mode. *risk* is the registry tier
    ('safe'/'moderate'/'risky' — unknown tools default risky upstream)."""
    if risk == "safe":
        return PASS
    if tool in ("write_file", "edit_file"):
        if _is_plan_file(args.get("path"), plan_file, workdir):
            return Verdict("allow", "")
        return Verdict("deny", _WRITE_MSG.format(plan=plan_file))
    if tool == "bash":
        cmd = args.get("command")
        if isinstance(cmd, str) and is_read_only_command(cmd):
            return PASS
        return Verdict("deny", _BASH_MSG.format(plan=plan_file))
    if tool in _WORLD_READS:
        return PASS
    return Verdict("deny", _TOOL_MSG.format(tool=tool, plan=plan_file))


def _is_plan_file(path, plan_file: Path, workdir: Path) -> bool:
    """True iff *path* RESOLVES to exactly the plan file — `..`, symlinks, and
    absolute aliases all collapse first, so the carve-out can't be retargeted.
    Any malformed/missing path fails safe (False → deny)."""
    if not isinstance(path, str) or not path:
        return False
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p
    try:
        return p.resolve() == plan_file.resolve()
    except OSError:
        return False


# ── read-only bash classification ────────────────────────────────────────────
# Whitelist, not blacklist: only commands we KNOW don't mutate may pass to the
# ask flow; everything unrecognized is denied. Rejected outright: redirects,
# command chaining, substitution, and multi-line — so pipes are the only
# composition, and every pipe segment must itself be read-only.

_META = re.compile(r">|;|&|\$\(|`|<\(")
# awk/find can execute subcommands without any shell metacharacter
_EMBEDDED_EXEC = re.compile(r"\bsystem\s*\(")

_READ_CMDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "stat", "file", "du", "tree", "pwd",
    "which", "grep", "rg", "fd", "sort", "uniq", "cut", "diff", "realpath",
    "basename", "dirname", "date", "nl", "column", "od", "strings",
    "find", "sed", "awk", "git",
})
_GIT_READ_SUBS = frozenset({
    "log", "diff", "show", "status", "blame", "shortlog", "describe",
    "ls-files", "rev-parse", "grep", "reflog",
})
_FIND_WRITE_FLAGS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls",
})


def is_read_only_command(command: str) -> bool:
    """True iff *command* is a whitelisted read-only invocation (it still goes
    through the normal ask flow — this never auto-allows)."""
    cmd = command.strip()
    if not cmd or "\n" in cmd or _META.search(cmd) or _EMBEDDED_EXEC.search(cmd):
        return False
    return all(_segment_read_only(seg) for seg in cmd.split("|"))


def _segment_read_only(segment: str) -> bool:
    tokens = segment.split()
    # skip leading VAR=value assignments (FOO=1 git log)
    while tokens and re.match(r"^\w+=", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return False
    name = tokens[0].rsplit("/", 1)[-1]  # /usr/bin/git → git
    if name not in _READ_CMDS:
        return False
    if name == "git":
        return _git_read_only(tokens[1:])
    if name == "find":
        return not any(t in _FIND_WRITE_FLAGS for t in tokens[1:])
    if name == "sed":
        return not any(t.startswith("-i") for t in tokens[1:])
    return True


def _git_read_only(tokens: list[str]) -> bool:
    """git with a read-only subcommand. `-c` (can define alias/hook-ish config)
    and `--output`/`--ext-diff` (write a file / run a command) are rejected even
    on read subcommands."""
    sub = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-c",) or t.startswith("--output") or t == "--ext-diff":
            return False
        if t in ("-C", "--git-dir", "--work-tree"):
            i += 2  # flag takes a value
            continue
        if t.startswith("-"):
            i += 1
            continue
        sub = t
        break
    if sub is None or sub not in _GIT_READ_SUBS:
        return False
    # write-capable flags can appear after the subcommand too (git log --output=x)
    return not any(t.startswith("--output") or t == "--ext-diff" for t in tokens[i:])
