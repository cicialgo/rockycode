"""Memory M0 smoke test: store roundtrip, archive-not-delete, prompt
section, recall/remember tools. No API, no Docker."""
import asyncio
import os
import tempfile
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine.tools import execute
from rockycode.memory import Memory, MemoryStore, build_memory_tools, memory_prompt_section
from rockycode.memory.store import parse_memory, to_markdown

store = MemoryStore.for_workdir(Path.cwd())

# ---- roundtrip ---------------------------------------------------------------
fact = Memory(
    name="tests-are-smoke-scripts",
    type="fact",
    description="pytest is not configured; tests/ holds smoke scripts",
    importance=7,
    evidence=["20260612-094815-b71f69bc"],
    triggers=["tests/**", "pytest"],
    body="Run `uv run python tests/smoke_engine.py` — there is no pytest setup.",
)
store.save(fact)
store.save(Memory(name="", type="feedback", description="never use ANSI named colors",
                  body="Never use ANSI named colors; hex only (palette.py).", origin="user"))

loaded = store.load_all()
assert len(loaded) == 2, loaded
back = store.get("tests-are-smoke-scripts")
assert back.triggers == ["tests/**", "pytest"], back.triggers
assert back.evidence == ["20260612-094815-b71f69bc"], back.evidence
assert back.importance == 7 and back.status == "active" and back.created, back

# parse is lenient: a bare markdown file with no frontmatter is a valid memory
bare = parse_memory("just a plain note, no frontmatter")
assert bare.type == "fact" and bare.body.startswith("just a plain"), bare
assert parse_memory(to_markdown(back)).body == back.body

# ---- prompt section ----------------------------------------------------------
(store.root / "MEMORY.md").write_text("- repo uses uv, not pip\n")
section = memory_prompt_section(store)
assert "repo uses uv" in section                       # MEMORY.md inlined
assert "ANSI named colors" in section                  # feedback inlined in full
assert "tests-are-smoke-scripts [fact]" in section     # fact only as index line
assert "there is no pytest setup" not in section       # fact body NOT inlined
print("prompt section OK")

# ---- tools through the engine's execute() path --------------------------------
registry = {t.name: t for t in build_memory_tools(store)}


async def main():
    out, ok = await execute(registry, "recall_memory", '{"name": "tests-are-smoke-scripts"}')
    assert ok and "no pytest setup" in out, out
    out, ok = await execute(registry, "recall_memory", '{"name": "nope"}')
    assert not ok and "no memory named" in out, out
    out, ok = await execute(
        registry, "remember",
        '{"name": "uv-only", "content": "use uv run, never bare python", "type": "fact"}',
    )
    assert ok and "remembered 'uv-only'" in out, out
    assert store.get("uv-only").origin == "agent"
    print("tools OK")


asyncio.run(main())

# ---- archive, never delete -----------------------------------------------------
assert store.archive("uv-only")
assert not (store.root / "facts" / "uv-only.md").exists()
archived = store.get("uv-only")
assert archived is not None and archived.status == "archived"
assert (store.root / "archive" / "uv-only.md").exists()
assert all(m.name != "uv-only" for m in store.load_all())          # hidden by default
assert any(m.name == "uv-only" for m in store.load_all(include_archived=True))
assert not store.archive("uv-only")                                 # idempotent
print("archive OK")

# search
assert [m.name for m in store.search("ansi")], "search missed feedback body"
print("SMOKE OK — rocky remember everything. amaze!")
