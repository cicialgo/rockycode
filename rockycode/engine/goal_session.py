"""Frontend-agnostic orchestration of ONE goal run.

The sequence — isolated workspace → plan → derive permits from the plan →
provision the sandbox → run the milestone loop → summarize — is identical whether
it's driven from a terminal or from the in-app GoalScreen. This module owns that
sequence behind a small seam (the methods GoalScreen calls), so the UI only has
to render and collect y/e/n + edit text; it never touches Docker, git, or the
models directly. That also makes the screen testable with a fake backend.

Planning happens BEFORE the sandbox exists (it's LLM-only), so network/permits are
decided from the REAL plan, not a guess at the user's wording — same rule as the
CLI. `LiveGoalBackend` is the real implementation; tests substitute their own.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from rockycode.engine.budget import GoalBudget
from rockycode.engine.safety import Verdict, network_intent, pre_scan
from rockycode.engine.worktree import GoalWorkspace
from rockycode.pricing import UsageLedger


@dataclass
class Permits:
    """What a plan will need, derived from the planner's REQUIRES line + a scan
    backstop over the milestones. `blocked` set → the plan must not run."""
    use_network: bool
    net_reason: str
    asks: List[Verdict]
    approved: set
    blocked: Optional[str] = None

    @property
    def needs_notice(self) -> bool:
        return bool(self.use_network or self.asks or self.net_reason)


@dataclass
class GoalSummary:
    status: str            # "done" | "budget" | "stalled" | "aborted" | "cancelled" | "error"
    reason: str
    milestones_done: int = 0
    milestones_total: int = 0
    branch: str = ""
    origin: str = ""
    workspace: str = ""
    log: str = ""
    currency: str = "usd"
    spend: float = 0.0
    base: str = ""

    @property
    def review_cmd(self) -> str:
        if not self.branch:
            return ""
        return f"git -C {self.origin} diff {self.base or 'HEAD'}..{self.branch}"

    @property
    def tidy_cmd(self) -> str:
        if not self.branch:
            return ""
        return f"git -C {self.origin} worktree remove {self.workspace}"


class LiveGoalBackend:
    """The real backend: git worktree isolation + Docker sandbox + EngineDriver.

    Lifecycle the screen drives:  setup() → plan() → [discuss()…] → run() → done.
    cleanup() is idempotent and safe to call on any exit path."""

    def __init__(
        self,
        objective: str,
        context: str = "",
        *,
        model: str,
        reviewer_model: str,
        budget: GoalBudget,
        workdir: Path,
        currency: str = "usd",
        network: Optional[bool] = None,
        review_every: int = 3,
        plan_file: Optional[Path] = None,
    ) -> None:
        self.objective = objective
        self.context = context
        # When set, plan() reads THIS already-approved plan file (from chat /plan)
        # instead of calling the LLM planner — the handoff skips re-planning.
        self.plan_file = Path(plan_file) if plan_file else None
        self.model = model
        self.reviewer_model = reviewer_model
        self.budget = budget
        self.workdir = Path(workdir).resolve()
        self.currency = currency
        self.network = network            # None → decide from the plan; True/False → forced
        self.review_every = review_every

        self.ledger = UsageLedger()
        self.ws: Optional[GoalWorkspace] = None
        self.driver = None
        self._client = None
        self._log_path: Optional[Path] = None
        self._sandbox = None

    # ---- lifecycle ----------------------------------------------------------

    async def setup(self) -> str:
        """Create the isolated workspace + the driver (no sandbox yet). Returns a
        one-line description of where the work will happen."""
        from openai import AsyncOpenAI

        from rockycode.engine.goal import EngineDriver
        from rockycode.onboarding import require_base_url, require_key

        slug = _time.strftime("%Y%m%d-%H%M%S")
        self.ws = GoalWorkspace.create(self.workdir, slug)
        log_dir = Path.home() / ".rockycode" / "goal-logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = log_dir / f"{slug}.log"
        except OSError:
            self._log_path = None
        self.log(f"# goal: {self.objective}\n# {slug}  workspace={self.ws.path}  branch={self.ws.branch}")

        # Explicit key AND endpoint, never the SDK's ambient env fallbacks.
        self._client = AsyncOpenAI(api_key=require_key(), base_url=require_base_url(),
                                   max_retries=5, timeout=300.0)
        from rockycode.engine.explore import make_goal_verifier
        self.driver = EngineDriver(
            client=self._client, model=self.model, reviewer_model=self.reviewer_model,
            workspace=self.ws, ledger=self.ledger, currency=self.currency,
            verifier=make_goal_verifier(client=self._client, model=self.model,
                                        workdir=self.ws.path, ledger=self.ledger),
        )
        where = str(self.ws.path) + (f"  ·  branch {self.ws.branch}" if self.ws.branch else "  (copy)")
        return where

    async def plan(self) -> tuple[list[str], str]:
        # Handoff from chat /plan: the plan is already drafted + approved. Read it
        # (shape-agnostic) and turn each phase into a milestone — no LLM re-plan.
        if self.plan_file is not None:
            from rockycode.engine.planmode import parse_plan_file
            try:
                text = self.plan_file.read_text(errors="replace")
            except OSError:
                text = ""
            phases = parse_plan_file(text)
            milestones = [
                (title + ("\n" + "\n".join(f"- {s}" for s in steps) if steps else ""))
                for title, steps in phases
            ]
            # Pass the FULL plan text as the requires-scan supplement so permits()
            # catches an install/network mention wherever it sits (a REQUIRES line,
            # or inside a step like "pip install requests"), not just in milestones.
            return milestones[:12], text
        plan_input = self.objective if not self.context else (
            f"{self.objective}\n\n[Context from the chat that led here — use it to inform "
            f"the plan; the objective above is the goal]:\n{self.context}")
        plan, requires = await self.driver.plan(plan_input)
        return plan, requires

    def permits(self, plan: list[str], requires: str) -> Permits:
        scan_text = "\n".join(plan) + (("\n" + requires) if requires else "")
        flags = pre_scan(scan_text)
        blocked = next((v.reason for v in flags if v.action == "block"), None)
        asks = [v for v in flags if v.action == "ask"]
        net_reason = network_intent(requires) or network_intent(scan_text)
        use_network = self.network if self.network is not None else bool(net_reason)
        return Permits(
            use_network=bool(use_network), net_reason=net_reason or "",
            asks=asks, approved={v.pattern for v in asks}, blocked=blocked,
        )

    async def discuss(self, plan: list[str], requires: str, msg: str) -> tuple[str, list[str], str]:
        """Answer a question about the plan and return (reply, plan, requires) —
        the plan may be revised. Errors become a short reply, never a crash."""
        try:
            return await self.driver.discuss(self.objective, plan, requires, msg)
        except Exception as e:  # noqa: BLE001
            return (f"(couldn't reason about that — {type(e).__name__})", plan, requires)

    async def run(
        self, plan: list[str], permits: Permits, on_event: Callable[[str], None]
    ) -> GoalSummary:
        """Provision the sandbox with the permit decision, attach the engine, and
        run the milestone loop. Always stops the sandbox; never raises for a normal
        run failure (returns an 'error' summary instead)."""
        from rockycode.engine.goal import GoalRunner, safe_bash_tool
        from rockycode.engine.loop import Engine
        from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry

        self.log("plan:\n" + "\n".join(f"  {i}. {m}" for i, m in enumerate(plan, 1)))
        self.budget.start()

        def emit(m: str) -> None:
            on_event(m)
            self.log(m)

        try:
            self._sandbox = await ChatSandbox.start(self.ws.path, network=permits.use_network)
        except Exception as e:  # noqa: BLE001
            return self._summary("error", f"sandbox (Docker) failed to start — {e}")

        try:
            reg = build_sandbox_registry(self._sandbox)
            reg["bash"] = safe_bash_tool(self._sandbox, permits.approved)
            engine = Engine(model=self.model, client=self._client, workdir=self.ws.path,
                            registry=reg, trajectory_meta={"goal": self.objective, "runner": "goal"})
            self.driver.attach(engine, network=permits.use_network)
            runner = GoalRunner(self.objective, self.driver, self.budget, self.ws, self.ledger,
                                review_every=self.review_every, on_event=emit, preplanned=plan)
            result = await runner.run()
            summary = self._summary(result.status, result.reason,
                                    result.milestones_done, result.milestones_total)
            self.log(f"result: {result.status} — {result.reason}  "
                     f"({result.milestones_done}/{result.milestones_total} milestones)")
            return summary
        except Exception as e:  # noqa: BLE001
            return self._summary("error", f"{type(e).__name__}: {e}")
        finally:
            try:
                await self._sandbox.stop()
            except Exception:  # noqa: BLE001
                pass
            self._sandbox = None

    async def cleanup(self, keep: bool) -> None:
        """Tidy the workspace. keep=True leaves the worktree + branch to review;
        keep=False removes an unstarted run (cancel / plan error)."""
        if self.ws is not None:
            try:
                self.ws.cleanup(keep=keep)
            except Exception:  # noqa: BLE001
                pass

    # ---- helpers ------------------------------------------------------------

    def _summary(self, status: str, reason: str, done: int = 0, total: int = 0) -> GoalSummary:
        ws = self.ws
        return GoalSummary(
            status=status, reason=reason, milestones_done=done, milestones_total=total,
            branch=(ws.branch if ws else ""), origin=(str(ws.origin) if ws else ""),
            workspace=(str(ws.path) if ws else ""), log=(str(self._log_path) if self._log_path else ""),
            currency=self.currency, spend=self.ledger.cost(self.currency),
            base=(getattr(ws, "base", "") or "" if ws else ""),
        )

    def log(self, msg: str) -> None:
        if self._log_path is None:
            return
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass
