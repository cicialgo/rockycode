#!/usr/bin/env python
"""REAL-API plan-mode canary (network, token-spending, skips without creds).

Drives the actual Engine (real DeepSeek model, real tools, plan gate on) with
the plan-mode marker and checks the LIVE model honors the protocol:

  - it drafts into exactly the plan file (the carve-out target),
  - the code it was asked to plan about stays byte-identical,
  - no stray files appear outside .rockycode/,
  - the turn ends (the marker's "then stop" is obeyed) within 2 turns.

A brainstorm question on turn 1 is fine — the canary answers "just write the
plan" once and expects the draft on turn 2. Unit tests already cover the gate
mechanics; this checks the MODEL side: marker-following on the real V4.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path.home() / ".rockycode" / ".env")
from rockycode.onboarding import load_credentials_into_env  # noqa: E402

load_credentials_into_env()

MODEL = (os.getenv("ROCKYCODE_MODEL") or "").strip()
if not MODEL or not (os.getenv("OPENAI_API_KEY") or "").strip():
    print("skip: OPENAI_API_KEY / ROCKYCODE_MODEL not set")
    sys.exit(0)

from rockycode.engine import planmode  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402

wd = Path(tempfile.mkdtemp(prefix="rockyplanlive-")).resolve()
buggy = "def add(a, b):\n    return a - b  # oops\n"
(wd / "mymath.py").write_text(buggy)
plan = planmode.create_plan_file(wd, topic="fix add")

eng = Engine(
    model=MODEL, workdir=wd,
    reasoning_effort="high", max_tokens=32_768,  # snappy canary, not a bench run
)
eng.plan_file = plan


async def turn(text: str) -> str:
    last = ""
    async for _ev in eng.run_turn(text):
        pass
    for m in reversed(eng.history):
        if m.get("role") == "assistant" and m.get("content"):
            last = m["content"]
            break
    return last


ASK = ("mymath.py's add() returns a-b instead of a+b. Plan the fix "
       "(include how to verify it).")
reply = asyncio.run(turn(planmode.marker(plan) + "\n\n" + ASK))
if not plan.read_text().strip():
    # turn 1 was a brainstorm question — answer it and expect the draft now
    print(f"turn 1 brainstormed: {reply[:100]!r}")
    reply = asyncio.run(turn(planmode.marker(plan) + "\n\njust write the plan."))

fails = []
content = plan.read_text().strip()
if not content:
    fails.append(f"model never drafted the plan file; last reply: {reply[:200]!r}")
elif "add" not in content.lower():
    fails.append(f"plan file content looks unrelated: {content[:200]!r}")
if (wd / "mymath.py").read_text() != buggy:
    fails.append("mymath.py was MODIFIED during plan mode")
stray = [p for p in wd.rglob("*")
         if p.is_file() and ".rockycode" not in p.parts and p.name != "mymath.py"]
if stray:
    fails.append(f"stray files created outside .rockycode/: {stray}")
denied = [m["content"][:80] for m in eng.history
          if m.get("role") == "tool" and "[blocked] plan mode" in m.get("content", "")]

if fails:
    print("FAIL:")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print(f"plan file ({len(content)} chars):")
for line in content.splitlines()[:12]:
    print("  │ " + line)
if denied:
    print(f"gate denials the model recovered from: {len(denied)}")
print("PLAN-MODE LIVE OK — real model drafts to the plan file, code untouched. amaze!")
