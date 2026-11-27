"""Grade a Lean proof by what was actually checked.

`lake env lean` is the repair loop. It is not a kernel recheck. Lean v4.35
ships `lake check`: build the default targets, export them, and replay the
export through `leanchecker` inside a Linux bubblewrap sandbox, failing on
any axiom outside `propext`, `Quot.sound`, and `Classical.choice`.
`lake check --paranoid` also runs the bundled external kernels
(`leanchecker-paranoid`, `lean4lean`, `nanoda`, `con-leche`, `con-ron`).

This module detects that command, chooses the exit-path invocation, and
maps the evidence to a verdict. It never adds `--inadvisably-no-sandbox`
unless the caller passes `allow_unsandboxed=True` after a real confirmation.
Detection follows the command, not a hardcoded version number.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

STANDARD_AXIOMS = ("propext", "Quot.sound", "Classical.choice")
NO_SANDBOX_FLAG = "--inadvisably-no-sandbox"

# `lake check --help` prints `lake check […]`. `\b` would also match
# `lake check-build`, so the command token has to end at whitespace, `[`, or EOS.
_CHECK_USAGE = re.compile(r"lake check(?:\s|\[|$)")
_PARANOID_FLAG = re.compile(r"--paranoid\b")
_UNKNOWN_COMMAND = re.compile(
    r"unknown command|unrecognized command|is not a lake command", re.I)
_TOOLCHAIN_VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class ToolchainProbe:
    """What this workspace's Lake can do. `lake_check` is the command probe."""

    toolchain: str
    lake_check: bool
    paranoid: bool
    sandbox: str  # "linux-bwrap" | "unavailable"
    detail: str

    @property
    def report_line(self) -> str:
        pin = self.toolchain or "lean-toolchain missing"
        return (
            f"toolchain={pin}, lake check={'yes' if self.lake_check else 'no'}, "
            f"sandbox={self.sandbox}"
        )


@dataclass(frozen=True)
class ExitPlan:
    """The one kernel command to run after the compile loop, if any."""

    action: str  # "run" | "compiled-only" | "needs-sandbox"
    argv: tuple[str, ...]
    strength_if_pass: str  # "compiled" | "kernel-checked" | "paranoid"
    unsandboxed: bool
    note: str


@dataclass(frozen=True)
class CheckResult:
    """One `lake check` / `lake comparator` invocation. `exit_code` None means
    it did not run, unless `incomplete` (timeout / resource — not a verdict)."""

    exit_code: Optional[int] = None
    output: str = ""
    incomplete: bool = False


@dataclass(frozen=True)
class Grade:
    verdict: str  # unverified | red | amber | green | disputed
    strength: str  # "" | compiled | kernel-checked | paranoid
    statement_locked: bool
    stop: bool  # kernel disagreed — do not repair
    repair: bool  # feed the log back into the compile budget
    proved: bool  # may the report say the claim was proved?
    label: str
    note: str


def read_lean_toolchain(workspace: Path) -> str:
    """First non-comment line of `lean-toolchain`, or "" if it is missing."""
    path = workspace / "lean-toolchain"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def toolchain_version_note(line: str) -> str:
    """Informational only. The command probe decides whether `lake check` exists."""
    match = _TOOLCHAIN_VERSION.search(line or "")
    if not match:
        return ""
    version = tuple(int(part) for part in match.groups())
    if version >= (4, 35, 0):
        return "lean-toolchain names v4.35 or newer; the command probe still decides"
    return "lean-toolchain is older than v4.35; `lake check` is not expected"


def sandbox_status(*, system: Optional[str] = None, bwrap_exists: Optional[bool] = None) -> str:
    """`linux-bwrap` only when this host can start Lake's sandbox.

    The sandbox is Linux-only and needs `bwrap` (bubblewrap) on `PATH`, or
    `COMPARATOR_BWRAP`. Other platforms, and Linux without bubblewrap, are
    `unavailable`. This function does not suggest turning the sandbox off.
    """
    host = platform.system() if system is None else system
    if host != "Linux":
        return "unavailable"
    if bwrap_exists is None:
        bwrap_exists = shutil.which("bwrap") is not None or shutil.which(
            _bwrap_override()) is not None
    return "linux-bwrap" if bwrap_exists else "unavailable"


def _bwrap_override() -> str:
    import os
    return os.environ.get("COMPARATOR_BWRAP", "")


def parse_check_help(exit_code: int, output: str) -> tuple[bool, bool]:
    """`(lake_check, paranoid)` from `lake check --help`.

    Unknown-command text loses even if the exit code is 0. `--paranoid` is
    reported only when `lake check` itself is present.
    """
    text = output or ""
    if _UNKNOWN_COMMAND.search(text):
        return False, False
    if exit_code != 0 or not _CHECK_USAGE.search(text):
        return False, False
    return True, _PARANOID_FLAG.search(text) is not None


def _default_runner(argv: list[str], env: Optional[dict]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, env=env, check=False,
        )
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return proc.returncode, f"{proc.stdout or ''}{proc.stderr or ''}"


def detect_toolchain(
    workspace: Path,
    *,
    runner: Optional[Runner] = None,
    lake_bin: str = "lake",
    env: Optional[dict] = None,
    system: Optional[str] = None,
    bwrap_exists: Optional[bool] = None,
) -> ToolchainProbe:
    """Probe `lake check --help`. A missing binary or an unknown subcommand
    is `lake_check=False`, not an error. Version text is recorded, not gated on.
    """
    toolchain = read_lean_toolchain(workspace)
    note = toolchain_version_note(toolchain)

    def run(argv: list[str]) -> tuple[int, str]:
        if runner is not None:
            return runner(argv)
        return _default_runner(argv, env)

    code, output = run([lake_bin, "check", "--help"])
    has_check, has_paranoid = parse_check_help(code, output)
    sandbox = sandbox_status(system=system, bwrap_exists=bwrap_exists)
    if has_check:
        detail = "lake check --help lists the kernel recheck"
        if has_paranoid:
            detail += " and --paranoid"
    elif code == 127 or "not found" in (output or "").lower():
        detail = "lake is not on PATH"
    else:
        detail = "this lake has no `check` subcommand"
    if note:
        detail = f"{detail}. {note}"
    return ToolchainProbe(
        toolchain=toolchain,
        lake_check=has_check,
        paranoid=has_paranoid,
        sandbox=sandbox,
        detail=detail,
    )


def plan_exit_check(
    probe: ToolchainProbe,
    *,
    paranoid: bool = False,
    allow_unsandboxed: bool = False,
) -> ExitPlan:
    """Choose the exit-path command. The repair loop stays on `lake env lean`.

    `allow_unsandboxed` defaults off. Passing it records an unsandboxed plan;
    it does not skip the permission prompt that the flag still requires.
    """
    if not probe.lake_check:
        return ExitPlan(
            "compiled-only", (), "compiled", False,
            "No `lake check` in this toolchain. Report green · compiled and say "
            "it was not kernel-rechecked. Do not invent a kernel verdict.",
        )
    want_paranoid = bool(paranoid and probe.paranoid)
    paranoid_note = ""
    if paranoid and not probe.paranoid:
        paranoid_note = " This `lake check` does not advertise --paranoid; stop at the official kernel."
    if probe.sandbox == "linux-bwrap":
        argv = ["lake", "check"]
        if want_paranoid:
            argv.append("--paranoid")
        strength = "paranoid" if want_paranoid else "kernel-checked"
        return ExitPlan(
            "run", tuple(argv), strength, False,
            "Run once after a sorry-free compile. Do not add "
            f"{NO_SANDBOX_FLAG}.{paranoid_note}",
        )
    if allow_unsandboxed:
        argv = ["lake", "check"]
        if want_paranoid:
            argv.append("--paranoid")
        argv.append(NO_SANDBOX_FLAG)
        strength = "paranoid" if want_paranoid else "kernel-checked"
        return ExitPlan(
            "run", tuple(argv), strength, True,
            "UNSANDBOXED. The user explicitly confirmed "
            f"{NO_SANDBOX_FLAG}. Label the report unsandboxed; Lake says this "
            "run is not trustworthy. This is not the default." + paranoid_note,
        )
    return ExitPlan(
        "needs-sandbox", (), "compiled", False,
        "`lake check` exists, but its bubblewrap sandbox is unavailable "
        "(Linux + `bwrap` only). Do not pass "
        f"{NO_SANDBOX_FLAG} unless the user explicitly confirms that flag. "
        "Alternatives: recheck an export on a Linux machine with bubblewrap "
        "(`lake check --from-export`), or report green · compiled and say the "
        "kernel recheck did not run." + paranoid_note,
    )


def module_root(rel: str) -> str:
    """`Rocky/Slug.lean` → `Rocky`. A file at the package root has no lib name."""
    rel = (rel or "").replace("\\", "/").lstrip("./")
    if "/" not in rel:
        return ""
    return rel.split("/", 1)[0]


def file_on_default_target(workspace: Path, rel: str) -> Optional[bool]:
    """Whether a lakefile names the library that contains `rel`.

    `lake check` replays default targets, not the single file `lake env lean`
    just compiled. True when the lakefile mentions that library name, False
    when a lakefile exists and does not, None when there is no lakefile or
    the file sits at the package root. None/False must not be reported as
    kernel-checked for this file.
    """
    files = [workspace / name for name in ("lakefile.toml", "lakefile.lean")]
    present = [path for path in files if path.is_file()]
    if not present:
        return None
    root = module_root(rel)
    if not root or root.endswith(".lean"):
        return None
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in present)
    # A comment that merely mentions the name is not a target. Require a
    # library declaration or a defaultTargets entry.
    quoted = rf'(?:"{re.escape(root)}"|\'{re.escape(root)}\')'
    declared = (
        re.search(rf"name\s*=\s*{quoted}", text) is not None
        or re.search(rf"\blean_lib\s+{re.escape(root)}\b", text) is not None
        or re.search(rf"defaultTargets\s*=\s*\[[^\]]*\"{re.escape(root)}\"", text) is not None
    )
    return True if declared else False


def comparator_config(
    theorem_names: list[str],
    *,
    challenge_module: str = "Challenge",
    solution_module: str = "Solution",
) -> dict:
    """`comparator.json` body. Permitted axioms are the three Lake treats as clean."""
    return {
        "challenge_module": challenge_module,
        "solution_module": solution_module,
        "theorem_names": list(theorem_names),
        "permitted_axioms": list(STANDARD_AXIOMS),
    }


def render_comparator_json(config: dict) -> str:
    return json.dumps(config, indent=2) + "\n"


def classify_check_failure(
    exit_code: Optional[int],
    output: str,
    *,
    incomplete: bool = False,
) -> str:
    """`pass | axiom | kernel | build | start-failed | unknown | incomplete | not-run`.

    Messages follow Lake v4.35 (`Lake/CLI/Check.lean`): a non-permitted axiom
    is `Axiom '…' is not permitted`, a checker rejection prints
    `kernel rejected`, and a failure to start is exit code 2. An unexplained
    exit 1 is `unknown`. The kernel-check grader treats that as disputed so
    the repair loop cannot route around a checker it does not understand.
    """
    if incomplete:
        return "incomplete"
    if exit_code is None:
        return "not-run"
    if exit_code == 0:
        return "pass"
    text = output or ""
    lowered = text.lower()
    if "kernel rejected" in lowered:
        return "kernel"
    if "Axiom" in text and "is not permitted" in text:
        return "axiom"
    if exit_code == 2 or _looks_like_start_failure(text):
        return "start-failed"
    if "Child exited" in text or "out of date oleans" in lowered:
        return "build"
    return "unknown"


def _looks_like_start_failure(text: str) -> bool:
    lowered = text.lower()
    return (
        "needs linux namespaces" in lowered
        or "lake-manifest.json" in lowered
        or "bubblewrap" in lowered
        or ("bwrap" in lowered and "not found" in lowered)
        or "no default target" in lowered
    )


def _excerpt(output: str, limit: int = 500) -> str:
    text = (output or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _compiled(note: str) -> Grade:
    return Grade(
        "green", "compiled", False, False, False, False, "green · compiled", note,
    )


def grade_proof(
    *,
    compile_ran: bool,
    compiled: bool,
    sorries: int = 0,
    admits: int = 0,
    axioms: int = 0,
    check_available: bool = False,
    check: Optional[CheckResult] = None,
    paranoid: Optional[CheckResult] = None,
    paranoid_included: bool = False,
    comparator: Optional[CheckResult] = None,
    file_in_check: Optional[bool] = None,
    skip_reason: str = "",
    unsandboxed: bool = False,
) -> Grade:
    """Map compile + audit + kernel evidence to one verdict.

    `proved` is true only for a sandboxed kernel acceptance of a file that
    was actually on the checked targets, and only when comparator (if run)
    agreed. A clean compile is `green · compiled` and is not proved.
    """
    if not compile_ran:
        return Grade(
            "unverified", "", False, False, False, False, "unverified",
            "No compile ran. This cannot be green or amber.",
        )
    if not compiled:
        return Grade(
            "red", "", False, False, True, False, "red",
            "`lake env lean` failed.",
        )
    if axioms:
        return Grade(
            "red", "", False, False, True, False, "red · axiom",
            "The file declares a new axiom. That is not a proof. Repair within "
            "the compile budget; do not report green.",
        )
    if sorries or admits:
        return Grade(
            "amber", "", False, False, True, False, "amber",
            f"{sorries} sorry, {admits} admit. The statement may be well-formed; "
            "it is not proved.",
        )

    if check is not None and check.incomplete:
        extra = ""
        if paranoid_included:
            extra = " Re-run `lake check` without --paranoid to record a kernel-checked result."
        return _compiled(
            "The kernel check did not finish (timeout or resources). That is not "
            "a rejection and it is not kernel-checked." + extra,
        )
    if check is None or check.exit_code is None:
        if not check_available:
            return _compiled(
                "This toolchain has no `lake check`. Report green · compiled and "
                "say it was not kernel-rechecked.",
            )
        if skip_reason == "sandbox":
            return _compiled(
                "`lake check` is installed but its bubblewrap sandbox is unavailable. "
                f"Do not pass {NO_SANDBOX_FLAG} unless the user explicitly confirms. "
                "Report green · compiled.",
            )
        return _compiled(
            "`lake check` is available and was not run. Do not report this as proved.",
        )

    kind = classify_check_failure(check.exit_code, check.output, incomplete=check.incomplete)
    if kind == "axiom":
        return Grade(
            "red", "", False, False, True, False, "red · nonstandard axiom",
            "A kernel recheck rejected a non-permitted axiom. Standard axioms are "
            "propext, Quot.sound, and Classical.choice. Feed this log back into "
            "the repair loop; do not report green.\n" + _excerpt(check.output),
        )
    if kind == "build":
        return Grade(
            "red", "", False, False, True, False, "red · check build",
            "The sandboxed `lake check` build failed. Repair from that log; "
            "do not report green.\n" + _excerpt(check.output),
        )
    if kind == "start-failed":
        return _compiled(
            "`lake check` could not start (sandbox, manifest, or default targets). "
            "This is not a kernel rejection. Do not pass "
            f"{NO_SANDBOX_FLAG} unless the user explicitly confirms.\n"
            + _excerpt(check.output),
        )
    if kind != "pass":
        # `unknown` is fail-closed: an exit 1 we cannot classify is not a
        # repair hint. Recognized kernel rejections stop the same way.
        return _disputed(check.output)

    if file_in_check is not True:
        why = (
            "this file is not on a default target"
            if file_in_check is False
            else "it is not confirmed that this file is a default target"
        )
        return _compiled(
            f"`lake check` passed for the project's default targets, but {why}. "
            "Do not call this file kernel-checked.",
        )

    if unsandboxed:
        graded = Grade(
            "green", "kernel-checked", False, False, False, False,
            "green · kernel-checked · unsandboxed",
            "The sandbox was disabled. Lake treats that run as not trustworthy. "
            "Do not report the claim as proved.",
        )
        return _apply_comparator(graded, comparator)

    separate = paranoid if not paranoid_included else None
    if separate is not None and separate.incomplete:
        graded = Grade(
            "green", "kernel-checked", False, False, False, True,
            "green · kernel-checked",
            "leanchecker accepted the export (standard axioms only). "
            "--paranoid did not finish; that is not a rejection and not a downgrade.",
        )
        return _apply_comparator(graded, comparator)
    if separate is not None and separate.exit_code is not None:
        pkind = classify_check_failure(separate.exit_code, separate.output)
        if pkind == "pass":
            graded = Grade(
                "green", "paranoid", False, False, False, True,
                "green · paranoid",
                "leanchecker and every bundled external kernel accepted the export.",
            )
            return _apply_comparator(graded, comparator)
        if pkind == "axiom":
            return Grade(
                "red", "", False, False, True, False, "red · nonstandard axiom",
                "A paranoid recheck rejected a non-permitted axiom. Repair within "
                "budget; do not report green.\n" + _excerpt(separate.output),
            )
        if pkind == "start-failed":
            graded = Grade(
                "green", "kernel-checked", False, False, False, True,
                "green · kernel-checked",
                "leanchecker accepted the export. --paranoid could not start; "
                "the official-kernel result still stands.\n" + _excerpt(separate.output),
            )
            return _apply_comparator(graded, comparator)
        return _disputed(separate.output)

    if paranoid_included:
        graded = Grade(
            "green", "paranoid", False, False, False, True, "green · paranoid",
            "leanchecker and every bundled external kernel accepted the export.",
        )
        return _apply_comparator(graded, comparator)
    graded = Grade(
        "green", "kernel-checked", False, False, False, True,
        "green · kernel-checked",
        "Sandboxed leanchecker accepted the export and only standard axioms are used.",
    )
    return _apply_comparator(graded, comparator)


def _disputed(output: str) -> Grade:
    return Grade(
        "disputed", "", False, True, False, False, "disputed",
        "The compiler accepted this development and a kernel rejected it. Stop. "
        "Preserve the Lean file and the full check log. Do not edit the proof "
        "to bypass the kernel. If kernels disagree, the user can report it to "
        "contact@lean-fro.org.\n" + _excerpt(output),
    )


def _apply_comparator(graded: Grade, comparator: Optional[CheckResult]) -> Grade:
    if graded.verdict != "green":
        return graded
    if comparator is None or (comparator.exit_code is None and not comparator.incomplete):
        return graded
    if comparator.incomplete:
        return replace(graded, note=graded.note + " comparator did not finish.")
    kind = classify_check_failure(comparator.exit_code, comparator.output)
    if kind == "pass":
        return replace(
            graded,
            statement_locked=True,
            label=graded.label + " · statement-locked",
            note=graded.note + " lake comparator accepted the agreed statement.",
        )
    if kind == "kernel":
        return _disputed(comparator.output)
    if kind == "start-failed":
        return replace(
            graded,
            note=graded.note + " comparator could not start, so the statement "
            "was not locked.\n" + _excerpt(comparator.output),
        )
    # Axiom, build, or an unclassified comparator rejection: the kernel
    # recheck may still stand, but the user's claim was not what was proved.
    if kind == "axiom":
        return Grade(
            "red", "", False, False, True, False, "red · nonstandard axiom",
            "comparator rejected a non-permitted axiom. Repair within budget; "
            "do not report the claim as proved.\n" + _excerpt(comparator.output),
        )
    return replace(
        graded,
        statement_locked=False,
        proved=False,
        note=graded.note + " comparator did not accept this as the agreed statement. "
        "Do not say the user's claim was proved.",
    )
