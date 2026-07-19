"""Routines — recurring, pre-approved autonomous work (self-evolve phase 2).

A routine is a directory under the global $ROCKYCODE_HOME/routines/<name>/:

  routine.toml   — the DECLARATION: what it does, when it's due, and the
                   grant envelope the user approved once on the enable card
                   (network, tools, isolation, budgets). Declarative and
                   hand-editable; never mutated by runs.
  SKILL.md       — the HOW: the playbook the runner follows.
  state.json     — the RUNTIME state: last run, lease spend, run history.
                   Machine-owned, separate on purpose so the declaration
                   stays reviewable.

Scheduling is catch-up-on-launch (no daemon): at launch, due routines show a
card — click to run. `auto = true` is a LEASE, not a switch (locked design,
2026-07-17): it expires after at most MAX_LEASE_DAYS or when the lease
budget is spent, whichever first, then the routine falls back to
click-to-run until the lease is renewed (one click, spend shown). Trust
decays; it must be re-earned.

Execution (slice 3) is exec-shaped: headless engine run where the approver
IS the grant envelope — out-of-grant means a clean stop and a card at next
launch, never a mid-run hang. Every run writes a trajectory with
project_id + runner="routine", so the dream grades routines like chats.
"""
from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_LEASE_DAYS = 7          # hard ceiling — a lease is never longer than this
CADENCES = {"daily": 86_400.0, "weekly": 7 * 86_400.0}


def routines_dir() -> Path:
    base = os.environ.get("ROCKYCODE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".rockycode"
    return root / "routines"


@dataclass
class Routine:
    name: str
    description: str = ""
    cadence: str = "daily"            # daily | weekly
    prompt: str = ""                  # the task line handed to the runner
    workdir: str = ""                 # where it runs
    project_id: str = ""              # groups its trajectories with a project
    output_dir: str = ""              # where results land (relative to workdir)
    # -- the grant envelope (approved once, on the enable card) --------------
    network: bool = False
    tools: list[str] = field(default_factory=list)
    isolation: bool = False           # worktree + branch delivery (mutating routines)
    budget_run: float = 0.10          # per-run spend cap (session currency)
    max_steps: int = 30               # per-run step cap (exec: never unbounded)
    # -- the auto lease -------------------------------------------------------
    auto: bool = False
    lease_deadline: float = 0.0       # unix; 0 = no lease ever granted
    budget_lease: float = 1.00        # cross-run cap while the lease is active
    enabled: bool = True
    path: Optional[Path] = None       # the routine's directory


def _emit_toml(r: Routine) -> str:
    def s(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [
        f"name = {s(r.name)}",
        f"description = {s(r.description)}",
        f"cadence = {s(r.cadence)}",
        f"prompt = {s(r.prompt)}",
        f"workdir = {s(r.workdir)}",
        f"project_id = {s(r.project_id)}",
        f"output_dir = {s(r.output_dir)}",
        f"network = {'true' if r.network else 'false'}",
        "tools = [" + ", ".join(s(t) for t in r.tools) + "]",
        f"isolation = {'true' if r.isolation else 'false'}",
        f"budget_run = {r.budget_run}",
        f"max_steps = {r.max_steps}",
        f"auto = {'true' if r.auto else 'false'}",
        f"lease_deadline = {r.lease_deadline}",
        f"budget_lease = {r.budget_lease}",
        f"enabled = {'true' if r.enabled else 'false'}",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class RoutineState:
    last_run: float = 0.0             # unix; 0 = never ran
    lease_spent: float = 0.0          # spend since the CURRENT lease started
    runs: list[dict] = field(default_factory=list)  # {sid, t, cost, status}


class RoutineStore:
    """Directory-per-routine under one global root. The toml is the contract,
    state.json the odometer — same files-are-the-truth rule as everything."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or routines_dir()

    # -- declarations ---------------------------------------------------------

    def list(self, project_id: Optional[str] = None) -> list[Routine]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            r = self.load(d.name)
            if r is None or not r.enabled:
                continue
            if project_id is None or r.project_id in ("", project_id):
                out.append(r)
        return out

    def load(self, name: str) -> Optional[Routine]:
        path = self.root / name / "routine.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return None
        known = {f for f in Routine.__dataclass_fields__ if f != "path"}
        clean = {k: v for k, v in data.items() if k in known}
        r = Routine(**{"name": name, **clean})
        if r.cadence not in CADENCES:
            r.cadence = "daily"
        r.path = self.root / name
        return r

    def save(self, r: Routine, skill_md: str = "") -> Path:
        d = self.root / r.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "routine.toml").write_text(_emit_toml(r), encoding="utf-8")
        if skill_md:
            (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
        r.path = d
        return d

    # -- runtime state --------------------------------------------------------

    def state(self, r: Routine) -> RoutineState:
        try:
            raw = json.loads((self.root / r.name / "state.json").read_text(encoding="utf-8"))
            return RoutineState(
                last_run=float(raw.get("last_run", 0.0)),
                lease_spent=float(raw.get("lease_spent", 0.0)),
                runs=list(raw.get("runs", [])),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return RoutineState()

    def _write_state(self, r: Routine, st: RoutineState) -> None:
        (self.root / r.name).mkdir(parents=True, exist_ok=True)
        (self.root / r.name / "state.json").write_text(json.dumps({
            "last_run": st.last_run, "lease_spent": st.lease_spent,
            "runs": st.runs[-50:],  # a bounded odometer, not a log store
        }, indent=1), encoding="utf-8")

    # -- due + lease ----------------------------------------------------------

    def due(self, project_id: Optional[str] = None, now: Optional[float] = None) -> list[Routine]:
        """Routines whose cadence has elapsed. Missed runs never stack — a
        routine is due once, no matter how long the machine slept."""
        now = time.time() if now is None else now
        out = []
        for r in self.list(project_id):
            st = self.state(r)
            if now - st.last_run >= CADENCES[r.cadence]:
                out.append(r)
        return out

    def lease_active(self, r: Routine, now: Optional[float] = None) -> bool:
        """The auto lease holds only while BOTH the deadline and the lease
        budget hold — expiry of either falls back to click-to-run."""
        now = time.time() if now is None else now
        if not (r.auto and r.enabled):
            return False
        if now >= r.lease_deadline:
            return False
        return self.state(r).lease_spent < r.budget_lease

    def grant_lease(self, r: Routine, days: float, budget: float) -> Routine:
        """Start (or renew) the auto lease: at most MAX_LEASE_DAYS, always
        with a budget. Renewal resets the lease odometer."""
        days = max(0.0, min(float(days), MAX_LEASE_DAYS))
        r.auto = True
        r.lease_deadline = time.time() + days * 86_400.0
        r.budget_lease = float(budget)
        st = self.state(r)
        st.lease_spent = 0.0
        self.save(r)
        self._write_state(r, st)
        return r

    def revoke_lease(self, r: Routine) -> Routine:
        r.auto = False
        r.lease_deadline = 0.0
        self.save(r)
        return r

    def record_run(self, r: Routine, *, session_id: str, cost: float, status: str,
                   now: Optional[float] = None) -> RoutineState:
        """Odometer tick after a run: last_run moves, lease spend accumulates,
        the run lands in the (bounded) history with its trajectory id — the
        dream finds the full story there."""
        now = time.time() if now is None else now
        st = self.state(r)
        st.last_run = now
        st.lease_spent += max(0.0, float(cost))
        st.runs.append({"sid": session_id, "t": now, "cost": cost, "status": status})
        self._write_state(r, st)
        return st


# ─────────────────────────────────────────────────────────────────────────────
# the runner — exec's headless machinery, driven by a routine's contract
# ─────────────────────────────────────────────────────────────────────────────


def _grant_tokens(tools: list[str]) -> frozenset[str]:
    """routine.toml lists what the user granted; exec's HeadlessApprover
    speaks grant tokens — bare tool names become "tool:<name>", anything
    already token-shaped (a bash safety-pattern name, "tool:x") passes as-is."""
    return frozenset(t if ":" in t or "-" in t else f"tool:{t}" for t in tools)


async def run_routine(store: RoutineStore, r: Routine, *, model: str,
                      client=None, registry=None, err=None) -> dict:
    """Run one routine through exec (sandboxed ALWAYS — no host fallback for
    unattended work; Docker down = the run fails loudly, the card says so).
    Settles the odometer from the result envelope and writes last-run.md
    beside the routine for the "ready" line. Returns a summary dict."""
    from rockycode.engine.headless import run_exec
    from rockycode.pricing import UsageLedger
    from rockycode.session import get_project

    workdir = Path(r.workdir).expanduser() if r.workdir else Path.cwd()
    skill = ""
    if r.path is not None:
        try:
            skill = (r.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            skill = ""
    out_note = (f"\nWrite your results into the directory '{r.output_dir}' "
                f"(relative to the working directory)." if r.output_dir else "")
    prompt = f"{r.prompt}{out_note}"
    if skill.strip():
        prompt += f"\n\nFollow this playbook:\n\n{skill.strip()}"

    project = get_project(workdir)
    lines: list[dict] = []
    code = await run_exec(
        prompt=prompt, model=model, workdir=workdir,
        grants=_grant_tokens(r.tools), max_steps=r.max_steps,
        originator=f"routine:{r.name}",
        sandbox=True, network=r.network,
        write=lines.append, client=client, registry=registry, err=err,
        extra_meta={"runner": "routine", "routine": r.name,
                    "project_id": r.project_id or project.id,
                    "project_name": project.name},
    )

    meta = next((l for l in lines if l.get("type") == "meta"), {})
    result = next((l for l in lines if l.get("type") == "result"), {})
    status = {0: "done", 2: "blocked", 3: "budget"}.get(code, "error")
    ledger = UsageLedger()
    if result.get("usage"):
        ledger.add(model, result["usage"])
    cost = ledger.cost("usd")
    store.record_run(r, session_id=meta.get("session", ""), cost=cost, status=status)

    summary = str(result.get("summary", "") or result.get("error", ""))
    if r.path is not None:
        try:
            (r.path / "last-run.md").write_text(
                f"# {r.name} — last run\n\nstatus: {status} · cost: ${cost:.4f} · "
                f"session: {meta.get('session', '?')}\n\n{summary}\n", encoding="utf-8")
        except OSError:
            pass
    return {"status": status, "cost": cost, "summary": summary,
            "session": meta.get("session", ""), "blocked_on": result.get("blocked_on")}
