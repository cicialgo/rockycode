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

import re
from dataclasses import dataclass


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

_ASK = [
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
