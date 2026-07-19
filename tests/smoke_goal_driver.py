"""Goal EngineDriver — the pure/testable parts: plan/verdict/review parsing and
the safety-gated bash. The model + Engine wiring needs a live run to validate."""
import asyncio

from rockycode.engine.goal import parse_plan, parse_review, parse_verdict, safe_bash_tool, split_plan

# --- plan parsing ---
assert parse_plan("1. Read code\n2. Fix bug\n3. Add test") == ["Read code", "Fix bug", "Add test"]
assert parse_plan("- Do X\n- Do Y") == ["Do X", "Do Y"]
assert parse_plan("Here is the plan:\nRefactor foo\nRun tests") == ["Refactor foo", "Run tests"]
assert len(parse_plan("\n".join(f"m{i}" for i in range(20)))) == 8  # capped at 8

# code the planner shouldn't emit is COLLAPSED, not exploded (real bug: a tkinter
# heredoc plan became one milestone per line of Python → 8 bogus milestones).
_heredoc = ("cat <<'EOF' > hello_gui.py\nimport tkinter as tk\nroot = tk.Tk()\n"
            "label = tk.Label(root, text='hi')\nlabel.pack()\nroot.mainloop()\nEOF")
_hd = parse_plan(_heredoc)
assert len(_hd) == 1 and "EOF" in _hd[0] and "tkinter" in _hd[0], f"here-doc must be 1 milestone: {_hd}"
# a fenced block folds into the milestone it belongs to (not new milestones)
_fenced = parse_plan("Create hello_gui.py with a Hello World window\n```python\nimport tkinter\n```\nAdd a README")
assert _fenced == ["Create hello_gui.py with a Hello World window\nimport tkinter", "Add a README"], _fenced
# a real second milestone after a here-doc still survives
_mixed = parse_plan("cat <<EOF > config.yaml\nport: 8080\nEOF\nWire config.yaml into startup")
assert len(_mixed) == 2 and _mixed[1] == "Wire config.yaml into startup", _mixed
print("parse_plan: here-doc / fence collapse (no per-line explosion)  ✓")

# --- verdict: only an explicit PASS advances (ambiguity → fail) ---
assert parse_verdict("PASS\nlooks good")[0] is True
assert parse_verdict("FAIL — tests red")[0] is False
assert parse_verdict("hmm, not sure")[0] is False
# model reasoned first, stated its call at the end (the live-run wasted retry)
assert parse_verdict("The baseline error is gone.\nRevised judgment: PASS")[0] is True
assert parse_verdict("Lots of reasoning...\nFinal decision: FAIL")[0] is False
assert parse_verdict("FAIL - but actually the objective is met")[0] is False  # led with FAIL → safe

# --- review: KEEP → None (unchanged), else a new plan ---
assert parse_review("KEEP") is None
assert parse_review("keep going, still on track") is None
assert parse_review("- New step A\n- New step B") == ["New step A", "New step B"]
# header/preamble lines are NOT milestones — the live-run leak where the reply
# header "REVISED remaining-milestone list" became a bogus [5] milestone
assert parse_review("REVISED remaining-milestone list\n- Do A\n- Do B") == ["Do A", "Do B"]
assert parse_review("REVISED remaining-milestone list") is None   # header only → nothing
assert parse_plan("Milestones\nAdd docstring\nRun linter") == ["Add docstring", "Run linter"]
# KEEP paraphrases → no re-plan (the live-run '[4] The plan is still good.' leak)
assert parse_review("The plan is still good.") is None
assert parse_review("Looks good — on track.") is None
assert parse_review("Fix the failing test") == ["Fix the failing test"]  # real 1-item plan kept
print("parsers: plan / verdict / review (+ header & keep-paraphrase guards)  ✓")

# --- split_plan: milestones + the planner's REQUIRES declaration ---
ms, req = split_plan("Add a docstring\nRemove import os\nREQUIRES: network (pip install requests)")
assert ms == ["Add a docstring", "Remove import os"], ms          # REQUIRES not a milestone
assert "network" in req and "pip install" in req, req
from rockycode.engine.safety import network_intent
assert network_intent(req), "REQUIRES: network must be detected"   # declare path
ms2, req2 = split_plan("Add a docstring\nRun the tests")
assert ms2 == ["Add a docstring", "Run the tests"] and req2 == "", (ms2, req2)
print("split_plan: milestones vs REQUIRES declaration  ✓")

# --- keyword-based planner context: rank files by the objective's keywords ---
import tempfile
from pathlib import Path as _Path

from rockycode.engine.goal import EngineDriver, _objective_keywords
from rockycode.pricing import UsageLedger as _UL
kw = _objective_keywords("Fix the auth login in the widget module")
assert {"auth", "login", "widget"} <= set(kw), kw
assert "the" not in kw and "fix" not in kw, kw          # stopwords dropped
_root = _Path(tempfile.mkdtemp())
for _i in range(60):
    (_root / f"mod_{_i:02d}.py").write_text("x = 1\n")           # noise, alphabetically first
(_root / "zzz_auth.py").write_text("def login(): return 1\n")   # relevant, alphabetically LAST
_drv = EngineDriver(client=None, model="m", reviewer_model="m",
                    workspace=type("W", (), {"path": _root})(), ledger=_UL())
_snap = _drv._workspace_snapshot("fix the auth login", max_files=10)
assert "zzz_auth.py" in _snap, "keyword-relevant file not surfaced (alpha-first would miss it)"
print("keyword planner context: keywords extracted + relevant file ranked in  ✓")


# --- safety-gated bash (fake sandbox) ---
class FakeSandbox:
    async def exec(self, script, **kw):
        return f"ran: {script}", 0


async def _bash_gate():
    fake = FakeSandbox()
    tool = safe_bash_tool(fake, approved_asks=frozenset())
    assert (await tool.fn("rm -rf /")).startswith("[blocked]")            # block: never
    assert (await tool.fn("git push origin main")).startswith("[blocked]")  # ask: unapproved
    assert "ran:" in await tool.fn("ls -la")                              # allow: runs
    # ask tier pre-approved for this run → runs
    tool2 = safe_bash_tool(fake, approved_asks=frozenset({"git-push"}))
    assert "ran:" in await tool2.fn("git push origin main")
    print("safety-gated bash: block refused, ask gated, allow + approved-ask run  ✓")


# --- EngineDriver.discuss: answer-then-plan parsing (pre-flight edit loop) ---
class _FakeClient:
    """Returns a canned completion so we can test discuss() parsing without an API."""
    def __init__(self, content):
        self.chat = type("Ch", (), {"completions": self})()
        self._c = content

    async def create(self, **kw):
        msg = type("M", (), {"content": self._c})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": None})()


async def _discuss():
    from rockycode.engine.goal import EngineDriver
    from rockycode.pricing import UsageLedger

    def drv(content):
        return EngineDriver(client=_FakeClient(content), model="m", reviewer_model="m",
                            workspace=None, ledger=UsageLedger())

    # a revision: answer + a new plan after ---PLAN---
    d = drv("No pip needed — all local edits.\n---PLAN---\n- Add docstring\n- Remove import os")
    reply, plan, req = await d.discuss("obj", ["old a", "old b"], "", "is there any pip install?")
    assert "No pip" in reply and plan == ["Add docstring", "Remove import os"], (reply, plan)

    # a question only (no ---PLAN---): answer returned, plan UNCHANGED
    d2 = drv("Nope, nothing installs — it stays offline.")
    reply2, plan2, req2 = await d2.discuss("obj", ["old a", "old b"], "", "any installs?")
    assert "offline" in reply2 and plan2 == ["old a", "old b"], (reply2, plan2)
    print("discuss: answer + revised plan / answer-only keeps plan  ✓")


asyncio.run(_bash_gate())
asyncio.run(_discuss())
print("GOAL DRIVER SMOKE OK — parsing + safety-gated bash. amaze!")
