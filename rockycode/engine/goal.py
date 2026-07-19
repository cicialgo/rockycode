"""Goal mode orchestrator: autonomous, budget-capped, sandbox-isolated runs.

Ties the phase-1/2 pieces together: safety (classify each bash command), budget
(stop on any cap), worktree (run on an isolated COPY of the repo). The loop:

  plan the objective into milestones
  → for each: work a turn, then VERIFY (explicit pass/fail via check_code)
  → every REVIEW_EVERY turns (or after repeated stalls) a milestone REVIEW —
    a (optionally stronger) model judges progress and can rewrite the remaining
    plan to keep the goal moving
  → finalize gracefully on: plan complete, any budget cap, or a hard stall.

The LLM-dependent steps (plan / work / verify / review) live behind the `Driver`
protocol, so this orchestration is fully testable with a fake driver; the real
one (EngineDriver) wires them to the agent Engine + models.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from rockycode.engine.budget import GoalBudget
from rockycode.engine.safety import Verdict, classify_command, pre_scan
from rockycode.engine.worktree import GoalWorkspace
from rockycode.pricing import UsageLedger


@dataclass
class GoalResult:
    status: str            # "done" | "budget" | "stalled" | "aborted"
    reason: str
    milestones_done: int
    milestones_total: int
    diff: str


class Driver(Protocol):
    # Returns (milestones, requires): the plan, plus the planner's optional
    # 'REQUIRES:' declaration text (network/push/sudo/install) for the pre-flight.
    async def plan(self, objective: str) -> tuple[list[str], str]: ...
    async def work(self, milestone: str, context: str) -> str: ...
    # Snapshot the project's check state BEFORE any work — so verify can tell a
    # pre-existing problem (not this milestone's fault) from a real regression.
    async def capture_baseline(self) -> None: ...
    async def verify(self, milestone: str, result: str) -> tuple[bool, str]: ...
    async def review(
        self, objective: str, remaining: list[str], done: list[str], note: str
    ) -> Optional[list[str]]: ...   # None = plan unchanged; a list = new remaining plan


@dataclass
class GoalRunner:
    objective: str
    driver: Driver
    budget: GoalBudget
    workspace: GoalWorkspace
    ledger: UsageLedger
    review_every: int = 3
    max_stalls: int = 2
    on_event: Optional[Callable[[str], None]] = None
    # Called with the ask-tier verdicts found at plan time; return True to allow
    # them for the run. Default (None) → deny (fail-safe): a headless run won't
    # silently escalate. Used only on the legacy (non-preplanned) path.
    on_approve: Optional[Callable[[list[Verdict]], Awaitable[bool]]] = None
    # When set, planning + pre-flight already happened upstream (the CLI plans
    # before the sandbox so network/permits are decided from the real plan, then
    # provisions and hands us the plan). We skip straight to execution.
    preplanned: Optional[list[str]] = None

    async def run(self) -> GoalResult:
        if self.preplanned is not None:
            plan = list(self.preplanned)
            total = len(plan)
        else:
            # Legacy path (tests / headless): plan + pre-flight here. The CLI uses
            # the preplanned path so it can decide network/permits from the plan
            # BEFORE the sandbox exists.
            self._emit(f"planning: {self.objective}")
            plan, _requires = await self.driver.plan(self.objective)
            total = len(plan)
            flags = pre_scan("\n".join(plan))
            blocked = [v for v in flags if v.action == "block"]
            if blocked:
                return GoalResult("aborted", f"plan names a blocked action: {blocked[0].reason}",
                                  0, total, "")
            asks = [v for v in flags if v.action == "ask"]
            if asks:
                approved = await self.on_approve(asks) if self.on_approve else False
                if not approved:
                    names = ", ".join(v.reason for v in asks)
                    return GoalResult("aborted", f"needs up-front approval for: {names}", 0, total, "")

        self.budget.start()
        self._emit(f"budget: {self.budget.preflight_note()}")
        # Snapshot the starting check state — planning doesn't touch files, so the
        # workspace is still pristine here. verify() judges each milestone against
        # this, so a pre-existing lint error a LATER milestone will fix can't fail
        # an earlier one.
        await self.driver.capture_baseline()
        done: list[str] = []
        stalls = 0
        turn = 0

        while plan:
            over = self.budget.exceeded(self.ledger)
            if over:
                return self._finalize("budget", over, done, plan)

            milestone = plan[0]
            turn += 1
            self._emit(f"[{turn}] working: {milestone}")
            result = await self.driver.work(milestone, self._context(done))
            ok, why = await self.driver.verify(milestone, result)

            if ok:
                self._emit(f"[{turn}] verified: {milestone}")
                # Checkpoint the verified work onto the goal branch. Durable +
                # crash-recoverable: a kill mid-run keeps every passed milestone.
                if self.workspace.commit(f"goal: {milestone}"):
                    self._emit(f"[{turn}] committed")
                done.append(plan.pop(0))
                stalls = 0
            else:
                stalls += 1
                self._emit(f"[{turn}] verify failed ({stalls}/{self.max_stalls}): {why}")
                if stalls >= self.max_stalls:
                    new_plan = await self.driver.review(self.objective, plan, done, why)
                    if new_plan is not None:
                        plan, total = new_plan, len(done) + len(new_plan)
                        stalls = 0
                        self._emit("reviewer re-planned after stall")
                    else:
                        return self._finalize("stalled", f"stuck on '{milestone}': {why}", done, plan)

            if turn % self.review_every == 0 and plan:
                new_plan = await self.driver.review(self.objective, plan, done, "periodic checkpoint")
                if new_plan is not None:
                    plan, total = new_plan, len(done) + len(new_plan)
                    self._emit("reviewer adjusted the plan")

        return self._finalize("done", "all milestones complete", done, plan)

    def _finalize(self, status: str, reason: str, done: list[str], remaining: list[str]) -> GoalResult:
        self._emit(f"finalizing: {status} — {reason}")
        return GoalResult(status, reason, len(done), len(done) + len(remaining), self.workspace.diff())

    def _context(self, done: list[str]) -> str:
        return "completed so far: " + "; ".join(done) if done else "(nothing done yet)"

    def _emit(self, msg: str) -> None:
        if self.on_event:
            self.on_event(msg)


# ─────────────────────────────────────────────────────────────────────────────
# EngineDriver — the real Driver: model calls + the agent Engine, safety-gated.
# The parse_* helpers and safe_bash_tool are pure and unit-tested; the model /
# Engine wiring needs a live run to validate end-to-end.
# ─────────────────────────────────────────────────────────────────────────────

_PLAN_SYS = (
    "You plan autonomous coding runs. You are given the objective and a snapshot "
    "of the actual project files — plan against what's REALLY there (real file and "
    "symbol names), never invent names. Break the objective into CONCRETE, "
    "individually VERIFIABLE milestones — as FEW as the task genuinely needs "
    "(a one-line change may be a single milestone; use more only for real scope, "
    "up to 8). Don't pad. Do NOT add a separate 'run the linter', 'make it pass', "
    "or 'verify it works/runs' milestone — passing the checks is an acceptance "
    "criterion checked automatically after EVERY milestone, not a step of its own. "
    "Each milestone is a PROSE description of WHAT to accomplish (e.g. 'Create "
    "hello_gui.py with a Tkinter window that shows a Hello World label') — NOT "
    "code. Never output source code, shell commands, here-docs, or file contents "
    "as milestones; the agent writes the code itself. One line = one milestone, so "
    "a single file is ONE milestone, not one per line. "
    "Output the milestones one per line, imperative, no numbering, no preamble. "
    "THEN, only if the plan needs elevated access the sandbox lacks by default, "
    "add ONE final line starting 'REQUIRES:' listing needs and why — from "
    "{network, git push, sudo, package install}. The sandbox is OFFLINE by "
    "default, so anything that installs packages or hits the internet REQUIRES "
    "network. If it needs none, omit the line."
)
_VERIFY_SYS = (
    "You verify whether a coding milestone's OBJECTIVE has been achieved. You get "
    "the milestone, the agent's summary, the checks NOW, and the BASELINE checks "
    "(captured before the run). Decide FIRST: the FIRST line must be exactly PASS "
    "or FAIL — no reasoning or hedging before it — then a one-line reason. Judge "
    "the END STATE, not what the agent did:\n"
    "• If the checks NOW are clean (no issues) and the objective is met, PASS. An "
    "error that was in the BASELINE but is GONE now was FIXED — that is SUCCESS, "
    "never an inconsistency or a discrepancy.\n"
    "• 'No issues found' / 'nothing to do' is CORRECT when the state is already "
    "clean (an earlier milestone may have handled it) — never fail an honest "
    "'nothing to do'.\n"
    "• An error present in BOTH baseline and now is pre-existing — it fails THIS "
    "milestone only if fixing it was this milestone's stated objective.\n"
    "• FAIL only if the objective is clearly unmet, or the checks NOW show a NEW "
    "error that is absent from the baseline (a regression this work introduced)."
)
_REVIEW_SYS = (
    "You review progress on an autonomous coding goal and keep it on track. Given "
    "the objective, what's done, what remains, and a note, decide: if the plan is "
    "still good, reply with the single word KEEP and nothing else. Otherwise reply "
    "with ONLY the revised remaining milestones, one per line — no heading, no "
    "numbering, no preamble. This replaces the remaining plan."
)
_DISCUSS_SYS = (
    "You're refining an autonomous coding plan WITH the user before it runs — talk "
    "to them like a colleague. You get the objective, the current milestone plan, "
    "and the user's message (a QUESTION or a change request). Reply in two parts:\n"
    "1) A SHORT, direct answer to the user (1–3 sentences): answer their question "
    "from the plan/objective, or acknowledge their change.\n"
    "2) Then a line containing exactly '---PLAN---', then the milestone list (one "
    "per line, imperative, no numbering) — REVISED if they asked for a change, "
    "otherwise the SAME plan unchanged. If it needs elevated access "
    "(network / package install / git push / sudo) add a final 'REQUIRES:' line."
)

# A heading / preamble line, not a milestone — e.g. 'REVISED remaining-milestone
# list', 'Here is the plan', 'Milestones', 'the new plan'. Milestones are
# imperative ('Add…', 'Run…') and never match this.
_HEADER_RX = re.compile(
    r"(?i)^\s*("
    r"here\b.*"
    r"|(the|a|an)?\s*(revised|updated|new)?\s*(remaining[-\s]?)?"
    r"(milestone|plan|step)s?[-\s]?(list)?\s*"
    r")$"
)


_FENCE_RX = re.compile(r"^\s*```")
# A heredoc opener: `<<EOF`, `<<-EOF`, `<< 'EOF'`, `<<"EOF"`. The body up to the
# delimiter is file CONTENT, not milestones — a planner that dumps
# `cat <<'EOF' > app.py … EOF` means ONE milestone (write the file), not one per
# line of the script. (Real bug: a tkinter heredoc became 8 per-line milestones.)
_HEREDOC_RX = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")


def parse_plan(text: str) -> list[str]:
    """Pull a milestone list out of a model reply (strips bullets/numbering, skips
    blank lines, trailing-colon headers, and preamble like 'Here is the plan').

    Code the planner shouldn't have emitted is collapsed, not exploded: a fenced
    ```block``` and a here-doc body are kept WITH their owning line as a single
    milestone instead of one milestone per line of code."""
    out: list[str] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and len(out) < 8:
        line = lines[i]
        i += 1
        # A bare code fence: swallow the whole fenced block (it's code, not steps).
        # Attach it to the previous milestone if any; otherwise drop it.
        if _FENCE_RX.match(line):
            block = []
            while i < n and not _FENCE_RX.match(lines[i]):
                block.append(lines[i]); i += 1
            i += 1  # closing fence
            if out and block:
                out[-1] = (out[-1] + "\n" + "\n".join(block)).strip()
            continue
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if not s or s.endswith((":", "：")) or _HEADER_RX.match(s):
            continue
        hd = _HEREDOC_RX.search(s)
        if hd:
            # One milestone spans the whole here-doc, delimiter included.
            delim, body = hd.group(2), [line]
            while i < n and lines[i].strip() != delim:
                body.append(lines[i]); i += 1
            if i < n:
                body.append(lines[i]); i += 1
            out.append("\n".join(body).strip())
        else:
            out.append(s)
    return out[:8]


_REQUIRES_RX = re.compile(r"(?i)^\s*(?:[-*•]\s*)?requires\s*[:：]\s*(.*)$")


def split_plan(reply: str) -> tuple[list[str], str]:
    """Split a plan reply into (milestones, requires-declaration). The planner may
    append one 'REQUIRES: network (why); ...' line — pulled out so it isn't treated
    as a milestone, and returned for the pre-flight approval scan."""
    requires = ""
    kept: list[str] = []
    for line in reply.splitlines():
        m = _REQUIRES_RX.match(line)
        if m:
            requires = m.group(1).strip()
            continue
        kept.append(line)
    return parse_plan("\n".join(kept)), requires


_VERDICT_MARK = re.compile(
    r"(?i)\b(?:revised\s+)?(?:judg?ment|verdict|conclusion|decision|final answer)"
    r"\s*[:\-]?\s*\**\s*(pass|fail)\b"
)


_PLAN_STOP = {
    "the", "a", "an", "and", "or", "to", "in", "of", "for", "with", "on", "at",
    "by", "add", "remove", "fix", "update", "make", "create", "run", "then", "it",
    "is", "that", "this", "use", "using", "into", "from", "so", "new", "all",
    "any", "not", "should", "need", "please", "file", "code", "function", "class",
    "test", "tests", "docstring",
}


def _objective_keywords(text: str) -> list[str]:
    """Salient words from the objective, for ranking which files to show the
    planner (drops stopwords + short tokens; keeps identifiers like snake_case)."""
    seen: set[str] = set()
    out: list[str] = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or ""):
        lw = w.lower()
        if lw in _PLAN_STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
    return out[:12]


def parse_verdict(text: str) -> tuple[bool, str]:
    """Read a verify reply. A clean PASS/FAIL on the first line wins. If the model
    reasoned first and stated its call at the end ('Revised judgment: PASS'), honor
    that explicit marker. Otherwise ambiguity → FAIL (never pass on a guess)."""
    body = text.strip()
    first = (body.splitlines() or [""])[0].strip().upper()
    if first.startswith("PASS") or first == "OK":
        return True, body
    if first.startswith("FAIL"):
        return False, body
    marks = _VERDICT_MARK.findall(body)  # 'judgment: PASS' when it reasoned first
    if marks:
        return marks[-1].lower() == "pass", body
    return False, body  # conservative: no clean verdict → FAIL


_KEEP_RX = re.compile(
    r"(?i)\b(keep|still (good|fine|on ?track|solid)|on track|no changes?|"
    r"unchanged|looks good|plan is (still )?(good|fine|ok|solid|sound))\b"
)


def parse_review(text: str) -> Optional[list[str]]:
    """Read a review reply: KEEP — or a paraphrase like 'the plan is still good'
    — → None (plan unchanged); a real milestone list → the new plan."""
    body = text.strip()
    if not body or re.match(r"(?i)keep\b", body):
        return None
    plan = parse_plan(body)
    # A reply that collapses to ≤1 line AND reads like an affirmation is a KEEP
    # paraphrase, not a one-item revised plan (the live-run '[4] The plan is
    # still good.' leak). A genuine revision is a real list of imperatives.
    if len(plan) <= 1 and _KEEP_RX.search(body):
        return None
    return plan or None


def safe_bash_tool(sandbox, approved_asks: frozenset):
    """A sandbox bash tool gated by the safety classifier for goal mode: block
    tier is always refused (the model must find a reversible path — it's on an
    isolated copy anyway); ask tier runs only if pre-approved for this run."""
    from rockycode.engine.sandbox import _bash as _sandbox_bash
    from rockycode.engine.tools import SCHEMAS, Tool

    async def bash(command: str) -> str:
        v = classify_command(command)
        if v.action == "block":
            return (f"[blocked] {v.reason}. Goal mode refuses this — find a reversible "
                    f"alternative (you're working on an isolated copy of the repo).")
        if v.action == "ask" and v.pattern not in approved_asks:
            return f"[blocked] {v.reason} — not pre-approved for this goal run."
        return await _sandbox_bash(sandbox, command)

    return Tool(name="bash", schema=SCHEMAS["bash"], fn=bash, risk="risky")


class EngineDriver:
    """Real Driver: plan/verify/review are (non-streaming) model calls; work
    drives the agent Engine one turn per milestone. Usage from every call flows
    into the shared ledger so the budget sees real spend."""

    def __init__(self, *, engine=None, client, model, reviewer_model, workspace,
                 ledger: UsageLedger, currency: str = "usd", network: bool = True,
                 verifier=None) -> None:
        self.engine = engine  # attached after the sandbox is provisioned (see attach)
        self.client = client
        self.model = model
        self.reviewer_model = reviewer_model
        self.workspace = workspace
        self.ledger = ledger
        self.currency = currency
        self.network = network  # False → tell the agent the sandbox is offline
        # Optional grounded verify (explore.make_goal_verifier): a read-only
        # child inspects the tree instead of judging from the worker's summary.
        # None (or any failure) → the original summary judge below.
        self.verifier = verifier
        self._baseline = ""  # check output before any work (see capture_baseline)

    def attach(self, engine, *, network: bool = True) -> None:
        """Wire the sandbox-bound engine after the pre-flight decision. The CLI
        plans before the sandbox exists (so network is decided from the plan),
        then provisions and attaches here."""
        self.engine = engine
        self.network = network

    async def _call(self, model: str, system: str, user: str, max_tokens: int = 2000) -> str:
        resp = await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        if resp.usage is not None:
            try:
                self.ledger.add(model, resp.usage.model_dump())
            except AttributeError:
                self.ledger.add(model, dict(resp.usage))
        return resp.choices[0].message.content or ""

    def _workspace_snapshot(self, objective: str = "", max_files: int = 40,
                            max_bytes: int = 6000, scan_cap: int = 800) -> str:
        """A compact view of the real project for the planner — the file tree plus
        small-file contents — so the plan targets ACTUAL names, not invented ones.
        When the repo has more than max_files, rank by the OBJECTIVE's keywords
        (filename + content) so a big repo still surfaces the RELEVANT code instead
        of the alphabetical first 40. Bounded for cost (scan_cap files; content
        only for small files)."""
        root = self.workspace.path
        skip = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build"}
        files: list = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            if p.is_file():
                files.append(p)
                if len(files) >= scan_cap:
                    break
        kws = _objective_keywords(objective)
        if kws and len(files) > max_files:
            def _score(p) -> int:
                name = p.name.lower()
                sc = 3 * sum(1 for k in kws if k in name)  # filename hit weighs most
                try:
                    if p.stat().st_size <= 40_000:
                        low = p.read_text(errors="ignore").lower()
                        sc += sum(1 for k in kws if k in low)
                except OSError:
                    pass
                return sc
            files.sort(key=_score, reverse=True)
            files = files[:max_files]
            files.sort()  # back to path order for a readable tree
        else:
            files = files[:max_files]
        lines = ["Project files:"] + [f"  {p.relative_to(root)}" for p in files]
        budget = max_bytes
        for p in files:
            if budget <= 0:
                break
            try:
                if p.stat().st_size > 4000:
                    continue
                text = p.read_text()
            except (OSError, UnicodeDecodeError):
                continue  # binary / unreadable — skip
            chunk = text[:budget]
            budget -= len(chunk)
            lines.append(f"\n--- {p.relative_to(root)} ---\n{chunk}")
        return "\n".join(lines)

    async def plan(self, objective: str) -> tuple[list[str], str]:
        user = f"Objective:\n{objective}\n\n{self._workspace_snapshot(objective)}"
        return split_plan(await self._call(self.model, _PLAN_SYS, user, max_tokens=2500))

    async def discuss(self, objective: str, plan: list[str], requires: str,
                      message: str) -> tuple[str, list[str], str]:
        """Talk about the plan with the user before it runs — return (reply, plan,
        requires): a short conversational answer plus the plan, revised if they
        asked, unchanged if they only asked a question."""
        plan_txt = "\n".join(f"- {m}" for m in plan)
        req_line = f"\nREQUIRES: {requires}" if requires else ""
        user = (f"Objective:\n{objective}\n\nCurrent plan:\n{plan_txt}{req_line}\n\n"
                f"The user says: {message}")
        out = await self._call(self.model, _DISCUSS_SYS, user, max_tokens=1500)
        chat, sep, plan_part = out.partition("---PLAN---")
        if sep and plan_part.strip():
            new_plan, new_req = split_plan(plan_part)
            if new_plan:
                return chat.strip(), new_plan, new_req
        return (chat or out).strip(), plan, requires  # answer only → plan unchanged

    async def work(self, milestone: str, context: str) -> str:
        from rockycode.engine.events import TextDelta, TurnFinished
        parts: list[str] = []
        offline = "" if self.network else (
            "\nThe sandbox has NO network — do not attempt package installs or "
            "downloads (pip/apt/npm/curl); use only what's already available.")
        prompt = (f"[goal] Work on this milestone: {milestone}\n{context}{offline}\n"
                  f"Make the changes, verify locally, then reply with a one-line summary.")
        async for ev in self.engine.run_turn(prompt):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, TurnFinished) and ev.usage:
                self.ledger.add(self.engine.model, ev.usage)
        return "".join(parts).strip()

    async def capture_baseline(self) -> None:
        """Snapshot the check output before any milestone runs, so verify can
        distinguish a pre-existing problem from a regression this run introduced."""
        from rockycode.engine.checks import run_checks
        self._baseline = await run_checks(self.workspace.path) or ""

    async def verify(self, milestone: str, result: str) -> tuple[bool, str]:
        from rockycode.engine.checks import run_checks
        checks = await run_checks(self.workspace.path) or "(no linter/type-checker configured)"
        if self.verifier is not None:
            try:
                return await self.verifier(
                    milestone=milestone, summary=result,
                    baseline=self._baseline, checks=checks,
                )
            except Exception:  # noqa: BLE001 — grounded verify degrades to the summary judge
                pass
        user = (
            f"Milestone: {milestone}\n\nAgent summary:\n{result}\n\n"
            f"Baseline checks (before the run began):\n{self._baseline or '(clean — no issues)'}\n\n"
            f"Checks now:\n{checks}"
        )
        return parse_verdict(await self._call(self.model, _VERIFY_SYS, user))

    async def review(self, objective: str, remaining: list[str], done: list[str], note: str) -> Optional[list[str]]:
        user = (f"Objective:\n{objective}\n\nDone:\n" + "\n".join(f"- {d}" for d in done) +
                "\n\nRemaining:\n" + "\n".join(f"- {r}" for r in remaining) + f"\n\nNote: {note}")
        return parse_review(await self._call(self.reviewer_model, _REVIEW_SYS, user))
