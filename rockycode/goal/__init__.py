"""Goal mode: autonomous, budget-capped, sandbox-isolated runs.

A host-level feature on top of the engine — the engine never imports it.
  runner.py    planner + runner + the review/verify parsers
  session.py   frontend-agnostic orchestration of ONE goal run
  review.py    chat tools for reviewing and merging a goal branch
  worktree.py  the isolated git worktree a run works on
  budget.py    token/cost caps for a run
"""
