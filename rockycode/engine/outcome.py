"""Heuristic per-session outcome signals — self-evolve phase 0.

Chat and goal trajectories never carried a reward: only bench wrote an
`outcome` record, so nothing downstream (dream's judge, RL export, skill
distillation) could tell a good session from a bad one. SessionStats
accumulates cheap deterministic counters at the exact branch points in the
engine loop, and Engine.finalize_outcome() flushes them as ONE `outcome`
record (source="heuristic") when the session ends.

Deliberately dumb: counters only, no model calls. The layered multi-angle
judge (source="judge") runs later, at dream time, over the whole transcript —
see the self-evolve design. Signals we do NOT count yet (reverted edits,
interrupts during streaming) are listed in TODO.md rather than half-measured
here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A bash command that runs tests. Deliberately coarse — heuristic outcome data
# feeds ranking and filtering, not ground truth, so a missed runner just means
# one uncounted test run. Matched against the RAW tool-arguments JSON.
TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|vitest|jest|mocha|cargo test|go test|"
    r"npm (?:run )?test|pnpm (?:run )?test|yarn (?:run )?test|make test|tox)\b"
)


@dataclass
class SessionStats:
    """Counters for one Engine lifetime (the whole session, every turn)."""

    turns: int = 0
    steps: int = 0            # API round-trips across all turns
    tool_calls: int = 0       # EXECUTED tool calls (denied ones counted below)
    tool_errors: int = 0      # harness-level failures: [error]/[timeout]/crash
    bash_nonzero: int = 0     # bash ran fine but the command exited non-zero
    denials: int = 0          # the user rejected a call at the approval prompt
    plan_denials: int = 0     # plan mode's read-only gate refused a mutation
    interrupts: int = 0       # Esc / new submit landed mid-tool-batch
    engine_errors: int = 0    # API failures + step-limit stops
    compactions: int = 0
    tests_run: int = 0
    tests_passed: int = 0     # bash test command that exited 0
    usage: dict[str, int] = field(default_factory=dict)

    def observe_tool(self, name: str, args_raw: str, output: str, ok: bool) -> None:
        """Record one EXECUTED tool call. Denials are counted separately by the
        loop — a rejection is a preference signal, not a tool failure."""
        self.tool_calls += 1
        if not ok:
            self.tool_errors += 1
        if name != "bash":
            return
        # bash reports "[exit N]" as its first line and execute() only marks
        # [error]/[timeout] as not-ok — a failing command is still ok=True, so
        # pass/fail must come from the exit status, not the ok flag.
        exit_zero = output.startswith("[exit 0]")
        if ok and not exit_zero:
            self.bash_nonzero += 1
        if TEST_CMD_RE.search(args_raw or ""):
            self.tests_run += 1
            if ok and exit_zero:
                self.tests_passed += 1

    def as_data(self) -> dict:
        return {
            "turns": self.turns,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "bash_nonzero": self.bash_nonzero,
            "denials": self.denials,
            "plan_denials": self.plan_denials,
            "interrupts": self.interrupts,
            "engine_errors": self.engine_errors,
            "compactions": self.compactions,
            "tests": {"run": self.tests_run, "passed": self.tests_passed},
            "usage": dict(self.usage),
        }
