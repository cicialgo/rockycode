"""Pure permission policy — no UI, no I/O, no async. The testable heart of the
opt-in approval layer.

`decide(mode, risk, args, workdir)` maps a tool's static risk tier
(safe|moderate|risky — see tools.RISK / Tool.risk) and the session's permission
mode (yolo|ask|careful) to one of {"allow", "ask"}. The TUI's approver runs this
first and only pops a modal on "ask"; bench/headless never even calls it (its
engine keeps the always-allow default).

`sniff_danger(tool, args)` is an advisory heuristic that flags remote-code-
execution / destructive patterns so the approval modal can highlight them — the
real defense against a "cheating" skill that tells the model to curl a virus,
applied at the layer where the actual command is visible.

The verifier (`engine/verifier.py`) may run only after `decide` returns `"ask"`.
It never overrides `"block"` or `"allow"`, and it stays off unless configured.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from rockycode.engine.safety import (
    KEYCHAIN_READ_WHY, LAKE_NO_SANDBOX_WHY, ROCKY_STORE_WHY, classify_command, disables_lake_sandbox,
    names_rocky_store, reads_keychain,
)
from rockycode.engine.file_access import READ_TOOLS, read_target

MODES = ("yolo", "ask", "careful")
RISKS = ("safe", "moderate", "risky")

# The order the TUI's shift+tab walks: tightest → loosest, then wrap. One step
# always LOOSENS, which is the only direction a shortcut can be predictable in;
# the wrap lands on `careful`, so a run of keypresses never parks you in yolo by
# accident. Lives here (not in the TUI) so the policy layer owns the ordering
# and tests can assert it without a terminal.
CYCLE = ("careful", "ask", "yolo")


def next_mode(mode: str, step: int = 1) -> str:
    """The mode `step` notches along CYCLE, wrapping. Unknown mode → CYCLE[0]."""
    try:
        i = CYCLE.index(mode)
    except ValueError:
        return CYCLE[0]
    return CYCLE[(i + step) % len(CYCLE)]


# human_only()'s tokens, in words: the line an approval card and a headless refusal show.
HUMAN_ONLY_WHY = {
    "lake-no-sandbox": LAKE_NO_SANDBOX_WHY,
    "rocky-store": ROCKY_STORE_WHY,
    "keychain-read": KEYCHAIN_READ_WHY,
    "rocky-settings": "changes rocky's own settings",
}


def human_only(tool: str, args: dict) -> str:
    """Why the person at the keyboard decides THIS call every time, or "".

    These calls reach past the task into rocky itself: turning off the lake
    sandbox, changing rocky's own settings (which model, which address a saved
    key is sent to), a shell command that names rocky's key file, a settings
    file or its whole folder, or asks the OS keychain for a password. They ask in every mode, yolo included; a session
    grant never covers them; the decision model never answers for the user. The
    token names the reason, and is the grant `exec --allow` takes for a command.
    """
    if tool == "bash":
        command = str(args.get("command", ""))
        if disables_lake_sandbox(command):
            return "lake-no-sandbox"
        if names_rocky_store(command):
            return "rocky-store"
        if reads_keychain(command):
            return "keychain-read"
    elif tool == "rocky_config":
        if str(args.get("action", "")).strip().lower() != "show":
            return "rocky-settings"
    return ""


def decide(mode: str, risk: str, args: dict, workdir: Path, tool: str = "", read_grants=()) -> str:
    """Return "allow" | "ask" | "block". Never prompts; just classifies the call.

    Command-level danger is judged FIRST, and it overrides the mode — this is the
    same per-command classifier goal mode uses, lifted into the shared permission
    layer so *chat* bash is judged by what the command DOES, not just that it's
    "a bash call". So a benign `ls` and a `brew install` are no longer the same
    decision:
    - a "block"-tier command (e.g. `sudo rm -rf /`, `curl … | sudo sh`) → "block"
      in EVERY mode, even yolo — it must never run unattended.
    - an "ask"-tier command (install / privileged / network: brew/apt/sudo/…) →
      "ask" in ask & careful — a fresh prompt every time. Paired with
      session_grantable(), a per-tool "allow for this session" can't wave it
      through. In yolo it still runs (you opted out of prompts).

    Then the ordinary tier logic:
    - yolo    : allow everything else
    - safe    : allow — EXCEPT a read whose target escapes the workdir (secrets)
    - risky   : ask (bash/web_fetch/mcp__*) in both ask and careful
    - moderate: `ask` allows an in-workdir write, else ask; `careful` always asks.

    A few asks survive yolo (human_only): `lake check` / `lake comparator` with
    `--inadvisably-no-sandbox`, a change to rocky's own settings, a shell command
    that names rocky's key or settings files (or its whole folder) or reads
    the OS keychain. Each is a fresh confirmation every time, not a mode default.
    """
    action = "allow"
    if tool == "bash":
        action = classify_command(str(args.get("command", ""))).action
        if action == "block":
            return "block"                     # never runs — even in yolo
    if human_only(tool, args):
        return "ask"                            # the person decides — even in yolo
    if action == "ask" and mode != "yolo":
        return "ask"                            # dangerous cmd → always a prompt
    if mode == "yolo":
        return "allow"
    if risk == "safe":
        if tool in READ_TOOLS and _read_escapes_workdir(tool, args, workdir, read_grants):
            return "ask"
        return "allow"
    if risk == "risky":
        return "ask"
    # moderate (write_file / edit_file / remember)
    if mode == "careful":
        return "ask"
    return "allow" if _writes_inside(args, workdir) else "ask"


def session_grantable(tool: str, args: dict) -> bool:
    """Whether a per-tool "allow for this session" grant may cover THIS call.

    False for a dangerous bash command (install / privileged / network /
    destructive): approving a benign `ls` for the session must NOT silently
    green-light a later `brew install` or `sudo …`. Those keep prompting (or
    stay blocked) every time, no matter the session allowlist. False as well for
    a human_only call: the lake binary grant covers `lake check` and `lake check
    --paranoid`, not the flag that turns the kernel sandbox off; approving one
    change to rocky's settings is not approving the next. Everything else is
    grantable as before."""
    if human_only(tool, args):
        return False
    if tool == "bash":
        return classify_command(str(args.get("command", ""))).action == "allow"
    return True


def command_binary(command: str) -> str:
    """The program a bash command runs — the unit a session grant is scoped to.

    A bash session grant is NEVER "all bash": approving `lake build` grants the
    `lake` BINARY (so `lake build`, `lake env lean`, `lake check`, … stop
    nagging during a proof/test loop) but a later `curl`/`rm`/`sudo` is a
    different binary and still prompts. The grant is also not every flag:
    `lake check --inadvisably-no-sandbox` is refused by session_grantable even
    after `lake` was allowed. Skips leading `VAR=val` assignments; returns the
    basename
    (so `/usr/bin/lake` and `lake` are the same grant).

    Returns "" for ANY command that chains, redirects, or substitutes
    (`; | & > < ( ) { } ` $` or a newline) — a compound like `lake build; curl
    evil` must not match a `lake` grant, or the chained command would ride in.
    "" never matches a grant, so those always re-prompt.
    """
    import re
    if any(c in command for c in "|&;<>(){}`$\n"):
        return ""
    for tok in command.strip().split():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            continue  # env assignment prefix
        return tok.rsplit("/", 1)[-1]
    return ""


def _read_escapes_workdir(tool: str, args: dict, workdir: Path, read_grants=()) -> bool:
    """Use the same canonical target as file tools, including glob prefixes."""
    try:
        target = read_target(tool, args, workdir)
        wd = workdir.resolve()
    except (OSError, ValueError, RuntimeError):
        return True
    if target is None:
        return True
    return not any(target == r or r in target.parents for r in (wd, *read_grants))


def _writes_inside(args: dict, workdir: Path) -> bool:
    """True only if args['path'] resolves to a location within workdir. Unknown
    or missing path is treated as outside (fail-safe → ask)."""
    p = args.get("path")
    if not isinstance(p, str) or not p:
        return False
    try:
        target = Path(p)
        if not target.is_absolute():
            target = workdir / target
        target = target.resolve()
        wd = workdir.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return target == wd or wd in target.parents


# Advisory only — these never block, they just surface a warning in the modal.
_DANGER = [
    (re.compile(r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.I),
     "pipes a download straight into a shell"),
    (re.compile(r"\|\s*(sudo\s+)?(ba|z|da)?sh\b(\s|$)", re.I),
     "pipes output into a shell"),
    (re.compile(r"base64\s+-+d\w*\b.*\|\s*(ba|z)?sh", re.I | re.S),
     "decodes base64 and runs it"),
    (re.compile(r"eval\s+[\"']?\$\(", re.I),
     "evals a command-substitution result"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(-{1,2}\S+\s+)*[~/]", re.I),
     "recursive delete of a home/root path"),
    (re.compile(r">>?\s*~?/?(\.(ssh|bashrc|zshrc|bash_profile|profile)\b|\.ssh/)", re.I),
     "writes to a shell/ssh dotfile"),
    (re.compile(r"\bcrontab\b", re.I),
     "edits cron jobs"),
    (re.compile(r"chmod\s+\+x\b[^&;|]*/tmp/", re.I),
     "makes a /tmp file executable"),
    (re.compile(r"\bnc\b[^|;&]*\s-\w*e\w*\b|/dev/tcp/", re.I),
     "looks like a reverse shell"),
    (re.compile(r"\b(?:ba|z|da)?sh\b\s+-[a-z]*c\b[^\n]*\$\(", re.I),
     "runs a command-substitution result in a shell"),
    (re.compile(r"<\(\s*(?:sudo\s+)?(?:curl|wget|fetch)\b", re.I),
     "process-substitutes a network download into a command"),
    (re.compile(r"\b(?:python[0-9.]*|perl|ruby|node|php)\b\s+-[a-z]*[ceE]\b[^\n]*"
                r"(?:curl|wget|urllib|urlopen|requests|socket|https?://)", re.I),
     "inline interpreter script that pulls from the network"),
]


# A settings change that moves where a provider's requests go. The key saved for
# that provider goes with them, so the card says it in words nobody can skim past.
KEY_ADDRESS_WARNING = "this provider's requests, and the key saved for it, will go to this address"


def sniff_danger(tool: str, args: dict) -> Optional[str]:
    """Return a short reason if the call matches a known dangerous pattern, else
    None. Checks bash commands, web_fetch URLs, and (defensively) MCP tool args."""
    if tool == "rocky_config":
        moves = str(args.get("action", "")).strip().lower() == "set_url"
        return KEY_ADDRESS_WARNING if moves and str(args.get("base_url", "")).strip() else None
    if tool == "bash":
        blob = str(args.get("command", ""))
        human = human_only(tool, args)
        if human in ("rocky-store", "keychain-read"):
            return HUMAN_ONLY_WHY[human]  # why a yolo session is asked at all
    elif tool == "web_fetch":
        blob = str(args.get("url", ""))
    elif tool.startswith("mcp__"):
        blob = str(args)
    else:
        return None
    if not blob:
        return None
    for rx, why in _DANGER:
        if rx.search(blob):
            return why
    return None
