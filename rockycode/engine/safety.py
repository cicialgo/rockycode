"""Command-safety classification for autonomous (goal) mode.

Goal mode runs unattended, and the sandbox mounts the project READ-WRITE, so a
destructive command reaches the user's real files. Every bash command is
classified BEFORE it runs, and the goal/plan is pre-scanned at start. Three
tiers (highest severity wins):

  block  — destructive / irreversible: rm -rf of a filesystem/home/mount root,
           mkfs, dd to a device, a fork bomb, shutdown. NEVER run in goal mode —
           the model gets an error and must find a reversible path. Isolation
           (goal runs on a COPY / git worktree) is the backstop so even a missed
           one can't touch the user's real repo.
  ask    — risky but sometimes legitimately needed: git push, sudo, curl|sh, a
           system OR language package install (apt/brew/pip/npm/cargo/… — they
           run code from a registry, so a poisoned/typosquat package is a real
           supply-chain risk). Surfaced at goal START for ONE up-front approval;
           if unapproved at runtime, treated as block.
  allow  — everything else, incl. normal dev (rm -rf build/, git commit, npm run,
           go build, pytest …).

This guards the agent against its own long-horizon mistakes; it is NOT a
security boundary against a hostile model (the container + copy isolation are).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Verdict:
    action: str   # "allow" | "ask" | "block"
    reason: str
    pattern: str


# rm -rf aimed at a filesystem/home/mount ROOT — not a named subdir. `rm -rf
# build/` stays allow; `rm -rf /`, `~`, `$HOME`, `/workspace`, `.`, `*` are block.
_RM_ROOT = re.compile(
    r"""\brm\b[^\n|;&]*\s
        (?:-\w+\s)*
        (?:
            /(?:\s|$|\*)                                   # bare /  or  /*
          | /(?:etc|var|usr|s?bin|lib\w*|boot|sys|proc|dev|opt|root|home|srv|run)(?:/|\s|$)
          | ~(?:/\s*\*?)?(?:\s|$)                          # ~  ~/  ~/*
          | \$HOME\b
          | /workspace(?:/\s*\*?)?(?:\s|$)
          | \.\s*$                                         # trailing .
          | \*\s*$                                         # trailing *
        )
    """,
    re.VERBOSE,
)

_BLOCK = [
    ("fork-bomb", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    ("disk-write", re.compile(r"\b(mkfs\w*|fdisk|parted)\b|\bdd\b[^\n]*\bof=/dev/"), "raw disk / filesystem write"),
    ("redirect-device", re.compile(r">\s*/dev/(sd|nvme|disk|mmcblk)\w+"), "redirect to a block device"),
    ("power", re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b"), "power / host-state change"),
    ("chmod-root", re.compile(r"\b(chmod|chown)\b[^\n]*\s-\w*R\w*\s[^\n]*\s(/|~|\$HOME|/workspace)(\s|$)"),
     "recursive perms/owner change on a filesystem root"),
]

# Lake's own name for turning off the bubblewrap sandbox around `lake check` /
# `lake comparator`. The project is untrusted input; this flag makes the run
# fully compromiseable. It must not ride along on a normal `lake` allow.
_LAKE_NO_SANDBOX = re.compile(r"--inadvisably-no-sandbox\b")
LAKE_NO_SANDBOX_WHY = "disables the lake check sandbox (--inadvisably-no-sandbox)"
KEYCHAIN_READ_WHY = "reads a saved password from the OS keychain"


def disables_lake_sandbox(command: str) -> bool:
    """True when a command passes Lake's `--inadvisably-no-sandbox` flag.

    That flag disables the sandbox `lake check` and `lake comparator` use to
    build and export an untrusted project. A session grant for the `lake`
    binary, yolo, and an auto-approver must not run it without a fresh
    confirmation.
    """
    return _LAKE_NO_SANDBOX.search(command or "") is not None


# rocky's own folder holds its key file and the settings that decide which
# address a saved key is sent to and which command describes an image. A shell
# command that names one of those files, or the folder as a whole, is not
# project work. What else lives there is ordinary: a global skill's script, an
# old session, a saved plan. `.rockycode-goal-*` worktrees and a project's own
# `.rockycode/plans` are not the folder at all.
_SETTINGS_FILE = r"(?:config|models|endpoints|providers)\.toml"
# The key and settings files under any spelling of the folder: a session started
# in the home directory reaches them as `.rockycode/…`.
_STORE_FILE = re.compile(r"(?<![\w-])\.rockycode/(?:\.env|" + _SETTINGS_FILE + r")(?![\w-])", re.IGNORECASE)
_ROCKY_HOME = r"(?:~|\$HOME\b|\$\{HOME\})/\.rockycode|\$ROCKYCODE_HOME\b|\$\{ROCKYCODE_HOME\}"
# After the folder's name: one plainly named thing inside it that is not a
# settings file, then anything below it. A dot-file, a wildcard, a variable or a
# brace in that first place can still be the key file, so none of them is plain.
_PLAIN_ITEM = re.compile(r"/(?!" + _SETTINGS_FILE + r")[A-Za-z0-9_][\w.-]*(?:[/:,].*)?", re.IGNORECASE)
ROCKY_STORE_WHY = "names rocky's key file, a settings file, or its whole folder"


def names_rocky_store(command: str) -> bool:
    """True when a shell command names rocky's key file, one of its settings
    files, or its folder as a whole (`~/.rockycode`, `$ROCKYCODE_HOME`, the
    folder's real path). A plainly named thing inside the folder is ordinary
    work: `~/.rockycode/skills/pdf/run.py`, `~/.rockycode/trajectories`.

    This reads the command's text. It stops the plain attempt (`cat
    ~/.rockycode/.env`) and the honest mistake; a command that builds the path
    another way is not caught here, and only a sandbox would stop it.
    """
    command = command or ""
    if _STORE_FILE.search(command):
        return True
    home = os.environ.get("ROCKYCODE_HOME") or str(Path.home() / ".rockycode")
    folder = "(?:" + _ROCKY_HOME + "|" + re.escape(home.rstrip("/")) + r")(?![\w-])"
    for match in re.finditer(folder, command, re.IGNORECASE):
        # The rest of this shell word, as the shell would read it: quotes and backslashes gone.
        rest = re.sub(r"[\\'\"`]", "", re.match(r"[^\s;|&<>()]*", command[match.end():]).group(0))
        if "/.." in rest or not _PLAIN_ITEM.fullmatch(rest):
            return True
    return False


# The tools that print a saved password, and rocky's own keychain entries by name.
_KEYCHAIN_READ = re.compile(
    r"\bsecurity\s+(?:find-(?:generic|internet)-password|dump-keychain)\b"
    r"|\bsecret-tool\s+(?:lookup|search)\b"
    r"|\bkeyring\s+get\b"
    r"|\bget_password\s*\(\s*['\"]rockycode['\"]"
)


def reads_keychain(command: str) -> bool:
    """True when a shell command asks the OS keychain for a saved password."""
    return _KEYCHAIN_READ.search(command or "") is not None


_ASK = [
    ("lake-no-sandbox", _LAKE_NO_SANDBOX, LAKE_NO_SANDBOX_WHY),
    ("keychain-read", _KEYCHAIN_READ, KEYCHAIN_READ_WHY),
    ("git-push", re.compile(r"(?:^|[\n;&|(])\s*git\b[^\n'\"]*\bpush\b"),
     "git push — publishes to a remote you can't review overnight"),
    ("privilege", re.compile(r"(?:^|[\s|;&(])sudo\b|(?:^|[\s|;&(])su\s"),
     "privilege escalation (sudo/su)"),
    ("remote-exec", re.compile(r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python\d?|perl|ruby|node)\b"),
     "pipes a network download into a shell/interpreter"),
    ("sys-install", re.compile(r"\b(apt|apt-get|yum|dnf|brew|pacman|snap)\b\s+(install|add|-S)\b"),
     "system package install"),
    # Language package installs fetch (and typically EXECUTE — setup.py, wheels,
    # npm lifecycle scripts) arbitrary code from a public registry: typosquat /
    # poisoned-package supply-chain risk. Gate them like a system install.
    ("pkg-install", re.compile(
        r"(?:^|[\s;&|(])(?:"
        r"(?:python[23]?\s+-m\s+)?pip[23]?\s+install"      # pip / pip3 / python -m pip install
        r"|uv\s+(?:pip\s+install|add)"
        r"|npm\s+(?:install|i|ci|add)(?:\s|$)"
        r"|(?:yarn|pnpm)\s+(?:add|install|i)(?:\s|$)"
        r"|gem\s+install"
        r"|cargo\s+(?:install|add)"
        r"|go\s+(?:install|get)(?:\s|$)"
        r"|poetry\s+(?:add|install)"
        r"|conda\s+install"
        r")", re.I),
     "language package install — runs code from a registry (supply-chain risk)"),
]


def classify_command(command: str) -> Verdict:
    """Classify a single bash command into allow / ask / block."""
    cmd = command.strip()
    if not cmd:
        return Verdict("allow", "", "")
    if re.search(r"\brm\b.*-\w*[rR]", cmd) and _RM_ROOT.search(cmd):
        return Verdict("block", "recursive force-remove of a filesystem/home/mount root", "rm-rf-root")
    for pat, rx, why in _BLOCK:
        if rx.search(cmd):
            return Verdict("block", why, pat)
    if names_rocky_store(cmd):  # more than a pattern: the folder's real path counts too
        return Verdict("ask", ROCKY_STORE_WHY, "rocky-store")
    for pat, rx, why in _ASK:
        if rx.search(cmd):
            return Verdict("ask", why, pat)
    return Verdict("allow", "", "")


def pre_scan(text: str) -> list[Verdict]:
    """Classify each line of a goal / plan; return the non-allow verdicts, deduped
    by pattern (block first, then ask). Used for the goal-start pre-flight, where
    'ask' verdicts become the one up-front approval batch."""
    seen: dict[str, Verdict] = {}
    for line in text.splitlines():
        v = classify_command(line)
        if v.action != "allow" and v.pattern not in seen:
            seen[v.pattern] = v
    return sorted(seen.values(), key=lambda v: 0 if v.action == "block" else 1)


# Network-intent hints in an objective/plan — used to surface "this goal may
# need network" in the up-front approval, so the offline-by-default goal sandbox
# can be opted online before the user leaves. Heuristic + conservative: a miss
# just means the run stays offline and a network step fails gracefully.
_NET_HINT = re.compile(
    r"(?i)\b("
    r"pip3?\s+install|(apt|apt-get|yum|dnf|brew|pacman|snap|conda)\s+(install|update|add|-S)|"
    r"npm\s+(i|install|ci)|yarn\s+add|pnpm\s+add|poetry\s+add|cargo\s+add|go\s+get|"
    r"download|install\s+(the\s+)?\w+\s+(package|librar|dependen|module|toolkit)|"
    r"\bclone\b|\bcurl\b|\bwget\b|https?://|\bpypi\b|from\s+the\s+internet|"
    r"fetch\s+(from|the)|\bnetwork\b|\binternet\b"
    r")\b"
)


def network_intent(text: str) -> str | None:
    """A short reason string if *text* (an objective or plan) looks like it needs
    network access, else None. Feeds the goal pre-flight network approval."""
    m = _NET_HINT.search(text or "")
    return m.group(0).strip() if m else None
