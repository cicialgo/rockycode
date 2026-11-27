"""Skills: reuse what users already have, zero migration.

Discovery, in priority order (first definition of a name wins):

  1. <project>/.claude/skills/<name>/SKILL.md   — Claude Code project scope
  2. <project>/.rockycode/skills/<name>/SKILL.md — rockycode's own
  3. ~/.rockycode/skills/<name>/SKILL.md        — rocky global ($ROCKYCODE_HOME;
                                                   dream-approved skills install here)
  4. ~/.claude/skills/<name>/SKILL.md           — Claude Code user scope
  5. ~/.codex/prompts/<name>.md                 — Codex custom prompts
  6. rockycode/skills/<name>/SKILL.md           — packaged built-ins, last on
     purpose: any project or user copy of the same name shadows a built-in

Progressive disclosure, same pattern as Claude Code: only `name — description`
lines go into the system prompt; the model calls the `skill` tool to load a
skill's full instructions on demand, so fifty installed skills cost fifty
lines of context, not fifty files.

SKILL.md frontmatter is parsed leniently (name:/description: lines between
--- markers) — no YAML dependency. Codex prompts have no frontmatter; the
filename is the name and the first body line is the description.

Chat-only, like MCP: bench never loads skills, so scores measure the harness.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rockycode.engine.tools import Tool, _truncate

MAX_SKILLS_IN_PROMPT = 100
MAX_DESCRIPTION_CHARS = 200

# Skills that ship inside the package (rockycode/skills/<name>/SKILL.md).
BUILTIN_DIR = Path(__file__).resolve().parents[1] / "skills"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# A YAML block scalar indicator on its own: `|`, `>`, with optional chomping/indent.
_BLOCK = re.compile(r"[|>][+-]?\d?")


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the markdown file
    source: str


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Lenient `key: value` frontmatter parse. Returns (fields, body)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    fields: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            fields[key] = value.strip().strip("'\"")
        elif key is not None and line.startswith((" ", "\t")) and line.strip():
            # An indented line continues the field above it: a YAML block scalar
            # (`description: |` or `>`) or a wrapped plain value. Its text is the
            # value — the indicator alone used to be shown, to the user and the model.
            head = fields[key]
            fields[key] = (("" if _BLOCK.fullmatch(head) else head + " ") + line.strip()).strip()
    for key, value in fields.items():
        if _BLOCK.fullmatch(value):  # an indicator with nothing under it
            fields[key] = ""
    return fields, text[m.end():]


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return "(no description)"


def _load_skill_md(path: Path, fallback_name: str, source: str) -> Optional[Skill]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    fields, body = _parse_frontmatter(text)
    return Skill(
        name=fields.get("name") or fallback_name,
        description=(fields.get("description") or _first_line(body))[:MAX_DESCRIPTION_CHARS],
        path=path,
        source=source,
    )


def discover_skills(workdir: Path, *, home: Optional[Path] = None,
                    builtin: bool = True) -> list[Skill]:
    """Collect skills from project + user locations, then packaged built-ins.
    `home=None` disables user-level lookup (tests); pass Path.home() for the
    real thing. `builtin=False` disables the packaged set (tests)."""
    found: dict[str, Skill] = {}

    def add(skill: Optional[Skill]) -> None:
        if skill is not None and skill.name not in found:
            found[skill.name] = skill

    def scan_skill_dirs(root: Path, source: str) -> None:
        if not root.is_dir():
            return
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if d.is_dir() and md.exists():
                add(_load_skill_md(md, d.name, source))

    scan_skill_dirs(workdir / ".claude" / "skills", "project .claude/skills")
    scan_skill_dirs(workdir / ".rockycode" / "skills", "project .rockycode/skills")
    if home is not None:
        # Rocky's own global store ($ROCKYCODE_HOME-aware, like trajectories) —
        # where dream-approved proposals install. After the project dirs on
        # purpose: a project skill always wins over a dream-drafted one.
        base = os.environ.get("ROCKYCODE_HOME")
        rocky_global = (Path(base).expanduser() if base else home / ".rockycode") / "skills"
        scan_skill_dirs(rocky_global, "~/.rockycode/skills")
        scan_skill_dirs(home / ".claude" / "skills", "~/.claude/skills")
        codex_prompts = home / ".codex" / "prompts"
        if codex_prompts.is_dir():
            for f in sorted(codex_prompts.glob("*.md")):
                add(_load_skill_md(f, f.stem, "~/.codex/prompts"))
    if builtin:
        scan_skill_dirs(BUILTIN_DIR, "built-in")

    return list(found.values())[:MAX_SKILLS_IN_PROMPT]


# ---- usage telemetry (evolve v2 watch loop) ------------------------------------
#
# A sidecar file, never the skill files themselves: per-skill load counts and
# timestamps drive LIFECYCLE decisions only (the dream's watch drafts a retire
# proposal for a dream-origin skill nobody ever loads) — content stays
# untouched. Best-effort everywhere: telemetry must never break a skill load.

def skill_usage_path() -> Path:
    base = os.environ.get("ROCKYCODE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".rockycode"
    return root / "skills" / ".usage.json"


def read_skill_usage() -> dict:
    try:
        data = json.loads(skill_usage_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def bump_skill_usage(name: str) -> None:
    try:
        path = skill_usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_skill_usage()
        entry = data.get(name) if isinstance(data.get(name), dict) else {}
        data[name] = {"loads": int(entry.get("loads", 0)) + 1, "last": time.time()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — telemetry is a nicety, loading is the job
        pass


def skills_prompt_section(skills: list[Skill]) -> str:
    lines = [f"- {s.name} — {s.description}" for s in skills]
    return (
        "\n\n# Skills available\n\n"
        "These are installed playbooks — rocky's built-ins plus the user's own. "
        "When a task matches one, call the "
        "`skill` tool with its name FIRST and follow its instructions.\n\n"
        + "\n".join(lines)
    )


def build_skill_tool(skills: list[Skill]) -> Tool:
    by_name = {s.name: s for s in skills}
    schema = {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "Load the full instructions of an installed skill. "
                "Call this before attempting a task that matches a skill's description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name from the 'Skills available' list.",
                        "enum": sorted(by_name),
                    },
                },
                "required": ["name"],
            },
        },
    }

    async def fn(name: str) -> str:
        skill = by_name.get(name)
        if skill is None:
            return f"[error] no skill named '{name}'. available: {', '.join(sorted(by_name))}"
        try:
            text = skill.path.read_text(errors="replace")
        except OSError as e:
            return f"[error] could not read skill: {e}"
        bump_skill_usage(skill.name)  # the watch loop's signal: this one earns its keep
        _, body = _parse_frontmatter(text)
        # The directory matters: skills often ship scripts referenced
        # relative to their own folder.
        return _truncate(
            f"# skill: {skill.name}\n# directory: {skill.path.parent}\n\n{body.strip()}"
        )

    # Loading a skill only returns SKILL.md text — it runs nothing. The real risk
    # is whatever bash/web_fetch the model then attempts, which is gated on its own.
    return Tool(name="skill", schema=schema, fn=fn, risk="safe")
