"""Lean v4.35 kernel recheck: detection, fallback, verdicts, and the sandbox flag.

No Lean install. A fake `lake` on PATH stands in for the toolchain probe.
"""
import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from rockycode.engine.approval import HeadlessApprover
from rockycode.engine.lean_verify import (
    CheckResult,
    classify_check_failure,
    comparator_config,
    detect_toolchain,
    file_on_default_target,
    grade_proof,
    parse_check_help,
    plan_exit_check,
    read_lean_toolchain,
    render_comparator_json,
    STANDARD_AXIOMS,
    NO_SANDBOX_FLAG,
    ToolchainProbe,
)
from rockycode.engine.permission import decide, session_grantable
from rockycode.engine.safety import classify_command
from rockycode.engine.verifier import VerifierGate

HELP = """\
Check this project against external checker(s)

USAGE:
  lake check [--from-export] [--paranoid] [--inadvisably-no-sandbox]

Builds the default build targets, exports them, and replays the result through
the kernel, erroring on any use of non-standard axioms.
"""

OLD_HELP = "error: unknown command 'check'\n"


def write_lake(directory: Path, body: str) -> Path:
    path = directory / "lake"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def probe_with(script: str, toolchain: str = "", *, system: str = "Linux", bwrap: bool = True):
    root = Path(tempfile.mkdtemp(prefix="rockylake-"))
    bindir = root / "bin"
    bindir.mkdir()
    write_lake(bindir, script)
    if toolchain:
        (root / "lean-toolchain").write_text(toolchain + "\n")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return detect_toolchain(root, env=env, system=system, bwrap_exists=bwrap)


# ── help text, not a version number, decides ────────────────────────────────
assert parse_check_help(0, HELP) == (True, True)
assert parse_check_help(1, OLD_HELP) == (False, False)
assert parse_check_help(0, "USAGE:\n  lake check-build\n") == (False, False)
assert parse_check_help(0, "USAGE:\n  lake check\n") == (True, False)
print("parse_check_help: check vs check-build, paranoid only when advertised  ✓")

modern = probe_with(
    "#!/bin/sh\nprintf '%s' " + repr(HELP) + "\n",
    "leanprover/lean4:v4.35.0",
)
assert modern.lake_check and modern.paranoid, modern
assert modern.toolchain == "leanprover/lean4:v4.35.0"
assert modern.sandbox == "linux-bwrap"
assert "v4.35" in modern.detail

old = probe_with(
    "#!/bin/sh\necho \"error: unknown command 'check'\" >&2\nexit 1\n",
    "leanprover/lean4:v4.31.0",
    system="Darwin",
    bwrap=False,
)
assert not old.lake_check and not old.paranoid, old
assert old.sandbox == "unavailable"
assert "older than v4.35" in old.detail

missing = probe_with("#!/bin/sh\nexit 127\n", system="Linux", bwrap=False)
assert not missing.lake_check and missing.sandbox == "unavailable"

# command probe wins over a version that merely looks new
nightly = probe_with(
    "#!/bin/sh\necho \"error: unknown command 'check'\" >&2\nexit 1\n",
    "leanprover/lean4:v4.99.0",
    system="Linux",
    bwrap=True,
)
assert not nightly.lake_check, nightly
print("detect: fake lake on PATH, version is a note, sandbox is linux+bwrap  ✓")

assert read_lean_toolchain(Path(tempfile.mkdtemp())) == ""
commented = Path(tempfile.mkdtemp())
(commented / "lean-toolchain").write_text("# comment\nleanprover/lean4:v4.35.0\n")
assert read_lean_toolchain(commented) == "leanprover/lean4:v4.35.0"
print("read_lean_toolchain: missing and comment skipped  ✓")

# ── exit plan never defaults to the no-sandbox flag ─────────────────────────
no_check = ToolchainProbe("", False, False, "unavailable", "")
plan = plan_exit_check(no_check, paranoid=True)
assert plan.action == "compiled-only" and plan.argv == () and NO_SANDBOX_FLAG not in plan.note

ready = ToolchainProbe("leanprover/lean4:v4.35.0", True, True, "linux-bwrap", "")
plain = plan_exit_check(ready)
assert plain.action == "run" and plain.argv == ("lake", "check") and plain.strength_if_pass == "kernel-checked"
para = plan_exit_check(ready, paranoid=True)
assert para.argv == ("lake", "check", "--paranoid") and para.unsandboxed is False

mac = ToolchainProbe("leanprover/lean4:v4.35.0", True, True, "unavailable", "")
blocked = plan_exit_check(mac, paranoid=True)
assert blocked.action == "needs-sandbox" and blocked.argv == ()
assert NO_SANDBOX_FLAG not in blocked.argv
assert "Do not pass" in blocked.note

forced = plan_exit_check(mac, paranoid=True, allow_unsandboxed=True)
assert forced.unsandboxed and forced.argv[-1] == NO_SANDBOX_FLAG
assert "--paranoid" in forced.argv
print("plan: sandbox on by default; no-sandbox only when explicitly allowed  ✓")

# ── which file a project check actually covers ───────────────────────────────
ws = Path(tempfile.mkdtemp())
assert file_on_default_target(ws, "Rocky/Slug.lean") is None
(ws / "lakefile.toml").write_text('name = "mathlib"\n[[lean_lib]]\nname = "Mathlib"\n')
assert file_on_default_target(ws, "Rocky/Slug.lean") is False
(ws / "lakefile.toml").write_text('[[lean_lib]]\nname = "Rocky"\n')
assert file_on_default_target(ws, "Rocky/Slug.lean") is True
(ws / "lakefile.toml").write_text('# Rocky is not a target\nname = "Mathlib"\n')
assert file_on_default_target(ws, "Rocky/Slug.lean") is False
(ws / "lakefile.lean").write_text('lean_lib Rocky\n')
assert file_on_default_target(ws, "Rocky/Slug.lean") is True
assert file_on_default_target(ws, "Slug.lean") is None
print("file_on_default_target: Rocky lib is opt-in, root files unknown  ✓")

cfg = comparator_config(["probe_thm"])
assert cfg["permitted_axioms"] == list(STANDARD_AXIOMS)
text = render_comparator_json(cfg)
assert "Classical.choice" in text and NO_SANDBOX_FLAG not in text
print("comparator.json: the three standard axioms, no sandbox flag  ✓")

# ── failure classes match Lake's messages ────────────────────────────────────
assert classify_check_failure(0, "Uses axioms: propext") == "pass"
assert classify_check_failure(
    1, "error: Axiom 'sorryAx' is not permitted; it is used by 'foo'") == "axiom"
assert classify_check_failure(
    1, "con-leche kernel rejected the solution\nerror: con-leche exited with 1") == "kernel"
assert classify_check_failure(
    1, "kernel rejected\nAxiom 'x' is not permitted") == "kernel"
assert classify_check_failure(
    2, "error: `lake check` sandboxes the code it checks with `bwrap`, which needs Linux namespaces.") == "start-failed"
assert classify_check_failure(1, "error: Child exited with 1") == "build"
assert classify_check_failure(1, "error: something unexpected") == "unknown"
assert classify_check_failure(None, "", incomplete=True) == "incomplete"
print("classify: axiom repairs, kernel rejection stops, unknown exit 1 stops  ✓")


def g(**kwargs):
    base = dict(compile_ran=True, compiled=True, check_available=True, file_in_check=True)
    base.update(kwargs)
    return grade_proof(**base)


assert grade_proof(compile_ran=False, compiled=False).verdict == "unverified"
assert grade_proof(compile_ran=False, compiled=False).proved is False
assert g(compiled=False).verdict == "red" and g(compiled=False).repair
assert g(sorries=1).verdict == "amber" and g(sorries=1).proved is False
assert g(axioms=1).label == "red · axiom" and g(axioms=1).repair

clean_old = grade_proof(compile_ran=True, compiled=True, check_available=False)
assert clean_old.label == "green · compiled" and clean_old.proved is False
assert "not kernel-rechecked" in clean_old.note

not_run = g()
assert not_run.label == "green · compiled" and not_run.proved is False
assert "was not run" in not_run.note

sand = g(skip_reason="sandbox", check=None)
assert sand.proved is False and NO_SANDBOX_FLAG in sand.note

passed = g(check=CheckResult(0, "Uses no axioms"))
assert passed.label == "green · kernel-checked" and passed.proved and not passed.stop

uncovered = g(check=CheckResult(0, "ok"), file_in_check=False)
assert uncovered.strength == "compiled" and uncovered.proved is False
unknown_file = g(check=CheckResult(0, "ok"), file_in_check=None)
assert unknown_file.proved is False

paranoid = g(check=CheckResult(0, "ok"), paranoid_included=True)
assert paranoid.label == "green · paranoid" and paranoid.proved

separate = g(check=CheckResult(0, "ok"), paranoid=CheckResult(0, "ok"))
assert separate.strength == "paranoid"

partial = g(check=CheckResult(0, "ok"), paranoid=CheckResult(incomplete=True))
assert partial.strength == "kernel-checked" and partial.proved and "did not finish" in partial.note

axiom = g(check=CheckResult(1, "error: Axiom 'foo' is not permitted; it is used by 'bar'"))
assert axiom.verdict == "red" and axiom.repair and not axiom.stop and axiom.proved is False

kernel = g(check=CheckResult(1, "Lean default kernel rejected the solution\nerror: Lean default exited with 1"))
assert kernel.verdict == "disputed" and kernel.stop and not kernel.repair and not kernel.proved
assert "contact@lean-fro.org" in kernel.note

mystery = g(check=CheckResult(1, "error: no idea"))
assert mystery.verdict == "disputed" and mystery.stop

started = g(check=CheckResult(2, "error: has no `lake-manifest.json`"))
assert started.verdict == "green" and started.strength == "compiled" and not started.stop

bare = g(check=CheckResult(0, "ok"), unsandboxed=True)
assert bare.proved is False and "unsandboxed" in bare.label

locked = g(check=CheckResult(0, "ok"), comparator=CheckResult(0, "Your solution is okay!"))
assert locked.statement_locked and locked.proved and "statement-locked" in locked.label

drift = g(check=CheckResult(0, "ok"), comparator=CheckResult(1, "error: statement mismatch"))
assert drift.proved is False and not drift.statement_locked and drift.verdict == "green"
not_started = g(check=CheckResult(0, "ok"), comparator=CheckResult(2, "error: could not read the configuration"))
assert not_started.proved and not not_started.statement_locked and "could not start" in not_started.note

comp_kernel = g(
    check=CheckResult(0, "ok"),
    comparator=CheckResult(1, "Lean default kernel rejected the solution"),
)
assert comp_kernel.verdict == "disputed" and comp_kernel.stop
print("grade: compiled is not proved; kernel reject stops; axiom repairs  ✓")

# ── permission: the flag is ask-tier and not a session grant ────────────────
verdict = classify_command("lake check --inadvisably-no-sandbox")
assert verdict.action == "ask" and verdict.pattern == "lake-no-sandbox", verdict
assert classify_command("lake check").action == "allow"
assert session_grantable("bash", {"command": "lake check --paranoid"}) is True
assert session_grantable("bash", {"command": "lake check --inadvisably-no-sandbox"}) is False
wd = Path(tempfile.mkdtemp())
assert decide("yolo", "risky", {"command": "lake check --inadvisably-no-sandbox"}, wd, tool="bash") == "ask"
assert decide("yolo", "risky", {"command": "lake check"}, wd, tool="bash") == "allow"

gate = VerifierGate()
review = asyncio.run(gate.review(
    mode="yolo", risk="risky", tool="bash", workdir=wd, session_allows=True,
    args={"command": "lake check --inadvisably-no-sandbox"},
))
assert review.effective == "ask" and review.applied is False and review.via == "lake-no-sandbox"
asked = asyncio.run(gate.on_ask(
    mode="ask", risk="risky", tool="bash",
    args={"command": "lake comparator --inadvisably-no-sandbox"},
))
assert asked.effective == "ask" and asked.consulted is False

headless = HeadlessApprover({}, frozenset())
assert asyncio.run(headless("bash", {"command": "lake check"})) is True
assert asyncio.run(headless("bash", {"command": "lake check --inadvisably-no-sandbox"})) is False
assert headless.blocked["grant"] == "lake-no-sandbox"
allowed = HeadlessApprover({}, frozenset({"lake-no-sandbox"}))
assert asyncio.run(allowed("bash", {"command": "lake check --inadvisably-no-sandbox"})) is True
print("permission: flag asks in yolo, blocks headless until lake-no-sandbox, Jev cannot allow  ✓")

root = Path(__file__).resolve().parents[1]
prove = (root / "rockycode/modes/research/prove.md").read_text()
skill = (root / "rockycode/skills/lean-prover/SKILL.md").read_text()
for body in (prove, skill):
    assert "lake env lean" in body and "lake check" in body
    assert "disputed" in body and "--inadvisably-no-sandbox" in body
    assert "kernel-checked" in body
assert "Do not pass" in skill
assert "never say \"proved\"" in prove and "never say \"proved\"" in skill
print("docs: prove mode and lean-prover both refuse compile-only proved  ✓")

print("LEAN TOOLCHAIN SMOKE OK — detect, fallback, verdicts, sandbox flag. amaze!")
