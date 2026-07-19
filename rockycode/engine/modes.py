"""Collaboration modes: how rocky holds a session, chosen by the USER.

A mode is a markdown contract that gets swapped into the system prompt
(engine.set_mode) — it shapes every turn's cadence and evidence rules, unlike
a skill, which the model pulls in per-task. Modes are grouped in FAMILIES;
each family is one slash command (/research, /learn) so there is never a pile
of commands to remember: bare command → picker, `<command> <type>` → direct.

Discovery (first definition of a name wins):
  1. <project>/.rockycode/modes/<family>/<name>.md — project-local
  2. rockycode/modes/<family>/<name>.md            — built-ins shipped with rocky

Project-local modes are visible in the picker but are NEVER auto-applied at
launch (a cloned repo must not inject prompt text silently — same trust rule
as config's permission key); only built-ins resolve from config's `mode`.

File shape — lenient frontmatter, like skills.py:

    ---
    name: deep-research
    description: one picker-row line
    ---
    First paragraph = the picker's when-to-use preview.
    Rest = the contract, addressed to rocky ("you").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "modes"
PROJECT_REL = Path(".rockycode") / "modes"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Mode:
    family: str        # "research" | "learn" | …
    name: str          # picker row + /research <name>
    description: str   # picker row one-liner
    preview: str       # when-to-use blurb (first body paragraph)
    body: str          # the contract that lands in the system prompt
    builtin: bool
    path: Path


def _parse(path: Path, family: str, builtin: bool) -> Optional[Mode]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    name, desc = path.stem, ""
    m = _FRONTMATTER.match(text)
    body = text[m.end():] if m else text
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "name" and v.strip():
                name = v.strip()
            elif k.strip() == "description":
                desc = v.strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()
             and not p.strip().startswith("#")]
    preview = paras[0] if paras else desc
    return Mode(family=family, name=name, description=desc, preview=preview,
                body=body.strip(), builtin=builtin, path=path)


def discover(workdir: Optional[Path] = None) -> dict[str, dict[str, "Mode"]]:
    """family → {name: Mode}. Project-local files shadow built-ins by name."""
    families: dict[str, dict[str, Mode]] = {}

    def scan(root: Path, builtin: bool) -> None:
        if not root.is_dir():
            return
        for fam_dir in sorted(root.iterdir()):
            if not fam_dir.is_dir():
                continue
            for f in sorted(fam_dir.glob("*.md")):
                mode = _parse(f, fam_dir.name, builtin)
                if mode is None:
                    continue
                families.setdefault(mode.family, {})
                # first definition wins — project scan runs before built-ins
                families[mode.family].setdefault(mode.name, mode)

    if workdir is not None:
        scan(Path(workdir) / PROJECT_REL, builtin=False)
    scan(BUILTIN_DIR, builtin=True)
    return families


def find_builtin(name: str) -> Optional[Mode]:
    """Exact-name lookup across families, built-ins only — the launch path for
    config's `mode` key (project-local modes are never auto-applied)."""
    for fam in discover(None).values():
        m = fam.get(name)
        if m is not None and m.builtin:
            return m
    return None


def resolve(family: str, token: str, *, workdir: Optional[Path] = None,
            builtin_only: bool = False) -> tuple[Optional[Mode], str]:
    """One mode from a user-typed name: exact, then unique prefix.
    Returns (mode, error) — exactly one is set."""
    modes = discover(workdir).get(family, {})
    if builtin_only:
        modes = {k: v for k, v in modes.items() if v.builtin}
    if not modes:
        return None, f"no {family} modes installed"
    t = token.strip().lower()
    if t in modes:
        return modes[t], ""
    hits = [m for n, m in modes.items() if n.startswith(t)]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, f"'{token}' is ambiguous — {', '.join(m.name for m in hits)}"
    return None, f"no {family} mode named '{token}' — try bare /{family} to browse"
