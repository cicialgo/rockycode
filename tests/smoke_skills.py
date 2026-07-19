"""Skills smoke test: discovery (Claude Code dirs + Codex prompts),
frontmatter parsing, dedup priority, prompt section, and the skill tool."""
import asyncio
import tempfile
from pathlib import Path

from rockycode.engine import tools as tools_mod
from rockycode.engine.skills import (
    build_skill_tool,
    discover_skills,
    skills_prompt_section,
)


async def main():
    wd = Path(tempfile.mkdtemp(prefix="rockyskills-wd-"))
    home = Path(tempfile.mkdtemp(prefix="rockyskills-home-"))

    # project-level Claude Code skill, full frontmatter
    deploy = wd / ".claude" / "skills" / "deploy-check"
    deploy.mkdir(parents=True)
    (deploy / "SKILL.md").write_text(
        "---\nname: deploy-check\ndescription: Verify a deploy is healthy before announcing it\n---\n"
        "# Deploy check\n\n1. run the smoke suite\n2. check the dashboard\n"
    )
    (deploy / "check.sh").write_text("echo ok\n")

    # user-level skill with the SAME name — project must win
    dupe = home / ".claude" / "skills" / "deploy-check"
    dupe.mkdir(parents=True)
    (dupe / "SKILL.md").write_text("---\nname: deploy-check\ndescription: USER VERSION\n---\nuser body\n")

    # user-level skill, no frontmatter (description = first line)
    notes = home / ".claude" / "skills" / "release-notes"
    notes.mkdir(parents=True)
    (notes / "SKILL.md").write_text("Write release notes from the git log.\n\nSteps...\n")

    # codex prompt
    codex = home / ".codex" / "prompts"
    codex.mkdir(parents=True)
    (codex / "fix-flaky.md").write_text("# Find and fix flaky tests\n\ninstructions here\n")

    skills = discover_skills(wd, home=home, builtin=False)
    by_name = {s.name: s for s in skills}
    assert set(by_name) == {"deploy-check", "release-notes", "fix-flaky"}, set(by_name)
    assert "USER VERSION" not in by_name["deploy-check"].description, "project must win dedup"
    assert by_name["release-notes"].description.startswith("Write release notes")
    assert by_name["fix-flaky"].source == "~/.codex/prompts"

    section = skills_prompt_section(skills)
    assert "deploy-check — Verify a deploy" in section and "`skill` tool" in section

    registry = {"skill": build_skill_tool(skills)}
    names = registry["skill"].schema["function"]["parameters"]["properties"]["name"]
    assert set(names["enum"]) == set(by_name)

    out, ok = await tools_mod.execute(registry, "skill", '{"name": "deploy-check"}')
    assert ok and "run the smoke suite" in out, out
    assert str(deploy) in out, "skill directory missing from output"
    assert "USER VERSION" not in out

    out, ok = await tools_mod.execute(registry, "skill", '{"name": "nope"}')
    assert not ok and "no skill named" in out, out

    # empty home / no skills → empty list, no crash
    assert discover_skills(Path(tempfile.mkdtemp()), home=None, builtin=False) == []

    # packaged built-ins: lean-prover ships inside rockycode/skills/
    builtins = discover_skills(Path(tempfile.mkdtemp()), home=None)
    b_by_name = {s.name: s for s in builtins}
    assert "lean-prover" in b_by_name, b_by_name
    assert b_by_name["lean-prover"].source == "built-in"
    assert "machine-verify" in b_by_name["lean-prover"].description
    # architecture-viz ships too, with its reference scaffold beside it
    assert "architecture-viz" in b_by_name, b_by_name
    assert b_by_name["architecture-viz"].source == "built-in"
    assert (b_by_name["architecture-viz"].path.parent / "template.html").exists(), \
        "architecture-viz must ship its template.html scaffold"

    # ...and anything project- or user-level with the same name shadows it
    shadow = wd / ".rockycode" / "skills" / "lean-prover"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(
        "---\nname: lean-prover\ndescription: PROJECT OVERRIDE\n---\nlocal body\n")
    shadowed = {s.name: s for s in discover_skills(wd, home=None)}
    assert shadowed["lean-prover"].description == "PROJECT OVERRIDE", "project must shadow built-in"

    # the skill tool serves a built-in's body like any other skill
    b_registry = {"skill": build_skill_tool(builtins)}
    out, ok = await tools_mod.execute(b_registry, "skill", '{"name": "lean-prover"}')
    assert ok and "lake env lean" in out and "never say \"proved\"".lower() in out.lower(), out[:200]

    # model layer: the TorchLean API excerpt ships beside SKILL.md and is referenced by it
    tl_doc = b_by_name["lean-prover"].path.parent / "torchlean-api.md"
    assert tl_doc.exists(), "torchlean-api.md must ship with the lean-prover skill"
    assert "torchlean-api.md" in out and "Model layer" in out, "SKILL.md must route to the excerpt"
    assert "open TorchLean" in tl_doc.read_text(), "excerpt must carry the namespace gotcha"

    print("skills:", [(s.name, s.source) for s in skills])
    print("built-ins:", [(s.name, s.source) for s in builtins])
    print("SKILLS SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
