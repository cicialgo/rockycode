"""Plan-mode gate: read-only policy for interactive planning. The plan file is
the ONE writable target; read-only bash still ASKS (never auto-allows);
everything mutating is denied with a teaching message. parse_plan_file reads
any plan shape the model writes (headings OR numbered) — the marker stays
format-light on purpose."""
import tempfile
from pathlib import Path

from rockycode.engine.planmode import gate, is_read_only_command, parse_plan_file

fails = []

# ── parse_plan_file: shape-agnostic (both shapes seen from the live model) ───
HEADING_STYLE = """# Fix: mymath.add() returns a-b instead of a+b

## Phase 1: Fix the operator
- In `mymath.py` line 2, change `return a - b` to `return a + b`.

## Phase 2: Verify
- Run the assert one-liner to confirm fix works.
- Run the edge-case asserts.
"""
NUMBERED_STYLE = """1. Fix the `add()` function in `mymath.py`
   - Change `return a - b` to `return a + b` on line 2.
   - Remove the `# oops` comment.

2. Verify the fix
   - Run the assert one-liner.
"""
BOLD_FLAT = """**1.** Fix the operator
**2.** Verify with asserts
"""
for label, text, want in [
    ("heading-style", HEADING_STYLE, [("Phase 1: Fix the operator", 1), ("Phase 2: Verify", 2)]),
    ("numbered-style", NUMBERED_STYLE, [("Fix the `add()` function in `mymath.py`", 2), ("Verify the fix", 1)]),
    ("bold-flat", BOLD_FLAT, [("Fix the operator", 0), ("Verify with asserts", 0)]),
]:
    got = [(t, len(steps)) for t, steps in parse_plan_file(text)]
    if got != want:
        fails.append(f"parse_plan_file {label}: {got} != {want}")
if parse_plan_file("no plan here, just prose.\n"):
    fails.append("prose should parse to zero phases")

# ── read-only bash classifier ────────────────────────────────────────────────
READ_ONLY = [
    "git log --oneline -20", "git diff HEAD~1", "git -C /Users/x/Code/opencode log",
    "git status", "git blame rockycode/cli.py", "git show 7869b7f --stat",
    "ls -la", "cat rockycode/engine/loop.py", "rg 'def gate' -n rockycode",
    "grep -rn approver rockycode/engine", "find . -name '*.py' -newer setup.py",
    "git log --format='%h %s' | head -20", "wc -l rockycode/engine/*.py",
    "FOO=1 git log", "/usr/bin/git log", "sed -n '1,40p' rockycode/config.py",
    "du -sh .rockycode", "tree -L 2",
]
NOT_READ_ONLY = [
    "echo done > STATUS.md",                      # redirect
    "git log > /tmp/log.txt",                     # redirect on a read subcommand
    "git commit -m wip", "git push", "git checkout -b x", "git stash",
    "git branch feature-x",                       # branch CREATES with an arg
    "git -c alias.x='!sh -c evil' x",             # -c config injection
    "git log --output=/tmp/exfil",                # write-capable flag after sub
    "rm -rf build", "npm install", "pip install requests",
    "python -c 'open(\"f\",\"w\").write(\"x\")'", # arbitrary interpreter
    "sed -i 's/a/b/' file.py",                    # in-place edit
    "find . -name '*.pyc' -delete", "find . -exec rm {} \\;",
    "cat a.txt && rm b.txt",                      # chaining
    "ls; rm -rf build",                           # chaining
    "cat $(echo secret)",                         # substitution
    "git log | tee /tmp/x",                       # pipe segment writes
    "git log | xargs rm",                         # pipe segment executes
    "awk 'BEGIN{system(\"rm -rf /\")}' f",        # embedded exec, no metachars
    "watch cat file", "xargs cat", "env cat f",   # command runners not whitelisted
    "line1\nrm -rf build",                        # multi-line
    "",
]
for c in READ_ONLY:
    if not is_read_only_command(c):
        fails.append(f"read-only REJECTED: {c!r}")
for c in NOT_READ_ONLY:
    if is_read_only_command(c):
        fails.append(f"mutating PASSED as read-only: {c!r}")

# ── gate() ───────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    wd = Path(td)
    plans = wd / ".rockycode" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "2026-07-06-planmode.md"
    plan.touch()

    def g(tool, args, risk):
        return gate(tool, args, risk, plan, wd)

    # safe-tier reads pass to the normal flow untouched
    for tool in ("read_file", "grep", "glob", "check_code", "web_search"):
        if g(tool, {"path": "x"}, "safe").action != "pass":
            fails.append(f"safe tool gated: {tool}")

    # the plan file is writable — relative, absolute, and ..-alias all allow
    for p in [".rockycode/plans/2026-07-06-planmode.md", str(plan),
              f".rockycode/plans/../plans/{plan.name}"]:
        v = g("write_file", {"path": p, "content": "# plan"}, "moderate")
        if v.action != "allow":
            fails.append(f"plan-file write not allowed: {p!r} -> {v.action}")
    if g("edit_file", {"path": str(plan), "old": "a", "new": "b"}, "moderate").action != "allow":
        fails.append("plan-file edit not allowed")

    # any other write is denied, with a message that names the plan file
    for p in ["rockycode/cli.py", "/tmp/evil.py", ".rockycode/plans/other.md", "", None]:
        v = g("write_file", {"path": p}, "moderate")
        if v.action != "deny" or plan.name not in v.message:
            fails.append(f"code write not denied properly: {p!r} -> {v.action}")

    # a symlink aliasing the plan file resolves and allows; one aliasing code denies
    link = wd / "plan-link.md"
    link.symlink_to(plan)
    if g("write_file", {"path": "plan-link.md"}, "moderate").action != "allow":
        fails.append("symlink to plan file should resolve to allow")
    (wd / "code.py").touch()
    trap = wd / "trap.md"
    trap.symlink_to(wd / "code.py")
    if g("write_file", {"path": "trap.md"}, "moderate").action != "deny":
        fails.append("symlink to code should deny")

    # bash: read-only classified → pass (still asks upstream); mutating → deny
    if g("bash", {"command": "git log --oneline"}, "risky").action != "pass":
        fails.append("read-only bash should pass to ask flow")
    if g("bash", {"command": "rm -rf build"}, "risky").action != "deny":
        fails.append("mutating bash should deny")
    if g("bash", {}, "risky").action != "deny":
        fails.append("bash with no command should deny (fail-safe)")

    # web_fetch reads the world → pass (still asks); other risky tools deny
    if g("web_fetch", {"url": "https://api-docs.deepseek.com"}, "risky").action != "pass":
        fails.append("web_fetch should pass to ask flow")
    for tool in ("mcp__thing__doit", "remember", "artifact_publish"):
        v = g(tool, {}, "risky")
        if v.action != "deny" or tool not in v.message:
            fails.append(f"risky tool not denied properly: {tool}")

if fails:
    print("FAIL:")
    for f in fails:
        print("  " + f)
    raise SystemExit(1)
print(f"PLANMODE SMOKE OK — {len(READ_ONLY)} read-only, {len(NOT_READ_ONLY)} mutating, "
      f"carve-out + symlinks + gate tiers clean. amaze!")
