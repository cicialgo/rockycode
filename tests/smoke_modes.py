"""Collaboration modes: discovery + frontmatter, engine prompt swap, launch
resolution, config persistence, and the /research command driven through the
TUI (headless pilot, fake engine — no network)."""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="rockytest-modes-home-")
os.environ["ROCKYCODE_HOME"] = _HOME

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
WORKDIR = Path(tempfile.mkdtemp(prefix="rockymodes-"))
os.chdir(WORKDIR)

from rockycode.engine import modes  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402

# discovery: built-ins ship with the package, families are directories
fams = modes.discover()
assert set(fams) >= {"research", "learn"}, set(fams)
assert set(fams["research"]) == {"deep-research", "paper-reading", "whiteboard", "prove"}, set(fams["research"])
assert set(fams["learn"]) == {"learn"}
dr = fams["research"]["deep-research"]
assert dr.builtin and dr.description and dr.preview and "# How you hold this session" in dr.body
assert not dr.preview.startswith("#"), "preview must be prose, not a heading"
print("discover: built-in families + frontmatter + preview  ✓")

# tool-awareness: each contract names real rocky tools
assert "web_research" in dr.body and "web_fetch" in dr.body
assert "read_file" in fams["learn"]["learn"].body
print("contracts: tool-aware (web_research/web_fetch/read_file named)  ✓")

# resolve: exact, unique prefix, ambiguous, unknown
m, err = modes.resolve("research", "deep-research")
assert m and m.name == "deep-research" and not err
m, err = modes.resolve("research", "wh")
assert m and m.name == "whiteboard", err
m, err = modes.resolve("research", "pa")
assert m and m.name == "paper-reading", err
m, err = modes.resolve("research", "pr")
assert m and m.name == "prove", err
m, err = modes.resolve("research", "p")
assert m is None and err, "ambiguous prefix (paper-reading vs prove) must not resolve"
m, err = modes.resolve("research", "zzz")
assert m is None and "no research mode" in err, err
print("resolve: exact / prefix / unknown  ✓")

# project-local modes: discovered (and shadow built-ins), excluded by builtin_only
local = WORKDIR / ".rockycode" / "modes" / "research"
local.mkdir(parents=True)
(local / "lean.md").write_text("---\nname: lean\ndescription: local lean mode\n---\nBe lean.\n")
fams2 = modes.discover(WORKDIR)
assert "lean" in fams2["research"] and not fams2["research"]["lean"].builtin
m, err = modes.resolve("research", "lean", workdir=WORKDIR, builtin_only=True)
assert m is None, "project-local mode must not resolve as a launch default"
assert modes.find_builtin("lean") is None and modes.find_builtin("learn") is not None
print("project-local: picker-visible, never a launch default  ✓")

# engine: set_mode swaps (not stacks), clear restores, trajectory notes it
client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
eng = Engine(model="fake", client=client, workdir=WORKDIR, system_prompt="BASE PROMPT")
assert eng.history[0]["content"] == "BASE PROMPT" and eng.mode_name is None
eng.set_mode("deep-research", dr.body)
assert eng.mode_name == "deep-research"
assert eng.history[0]["content"].startswith("BASE PROMPT")
assert "# Collaboration mode: deep-research" in eng.history[0]["content"]
eng.set_mode("learn", "TUTOR CONTRACT")
assert "TUTOR CONTRACT" in eng.history[0]["content"]
assert "deep-research" not in eng.history[0]["content"], "modes must replace, not stack"
eng.clear_mode()
assert eng.history[0]["content"] == "BASE PROMPT" and eng.mode_name is None
print("engine: set/replace/clear — system prompt swap, no stacking  ✓")

# resume: the dropped system prompt's mode is remembered (for the UI's
# "/research <x> to re-enter" hint), never silently forgotten
eng2 = Engine(model="fake", client=client, workdir=WORKDIR, system_prompt="BASE PROMPT")
eng2.resume([
    {"role": "system", "content": "OLD BASE\n\n# Collaboration mode: deep-research\n\ncontract"},
    {"role": "user", "content": "hi"},
])
assert eng2.resumed_mode == "deep-research", eng2.resumed_mode
assert eng2.mode_name is None, "the mode is offered back, not auto-applied"
assert eng2.history[0]["content"] == "BASE PROMPT"
eng3 = Engine(model="fake", client=client, workdir=WORKDIR, system_prompt="BASE PROMPT")
eng3.resume([{"role": "user", "content": "hi"}])
assert eng3.resumed_mode is None
print("engine: resumed session's mode remembered for the re-enter hint  ✓")

# config: `mode` key round-trips through the PROJECT file and load()
from rockycode import config  # noqa: E402

v, err = config.set_project_value(WORKDIR, "mode", "deep-research")
assert err is None and v == "deep-research"
assert config.load(WORKDIR)["mode"] == "deep-research"
v, err = config.set_project_value(WORKDIR, "mode", "")
assert err is None and config.load(WORKDIR)["mode"] == ""
print("config: project-scoped mode key round-trips  ✓")


# TUI: /research deep-research → chip lit + contract in the system prompt;
# /research off → chip dark + base prompt back
async def tui_flow():
    from rockycode.tui.app import ChatInput, RockyCodeApp

    eng2 = Engine(model="fake", client=client, workdir=WORKDIR, system_prompt="BASE PROMPT")
    app = RockyCodeApp(eng2, permission="yolo")
    async with app.run_test(size=(100, 30)) as pilot:
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/research deep-research"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert eng2.mode_name == "deep-research", eng2.mode_name
        assert "# Collaboration mode: deep-research" in eng2.history[0]["content"]
        from textual.widgets import Static
        chip = app.query_one("#modechip", Static)
        assert "deep-research" in str(chip.render()), chip.render()
        inp.text = "/research off"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert eng2.mode_name is None
        assert eng2.history[0]["content"] == "BASE PROMPT"
        assert "deep-research" not in str(chip.render())


asyncio.run(tui_flow())
print("tui: /research <type> lights the chip + swaps the prompt; off restores  ✓")


# bare /research opens the picker; esc cancels without changing anything
async def picker_flow():
    from rockycode.tui.app import ChatInput, RockyCodeApp
    from rockycode.tui.modepicker import ModePicker

    eng3 = Engine(model="fake", client=client, workdir=WORKDIR, system_prompt="BASE PROMPT")
    app = RockyCodeApp(eng3, permission="yolo")
    async with app.run_test(size=(100, 30)) as pilot:
        inp = app.query_one(ChatInput)
        inp.focus()
        inp.text = "/research"
        await pilot.press("enter")
        for _ in range(40):  # poll — the picker opens from a worker
            await pilot.pause(0.05)
            if isinstance(app.screen, ModePicker):
                break
        assert isinstance(app.screen, ModePicker), type(app.screen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ModePicker)
        assert eng3.mode_name is None and eng3.history[0]["content"] == "BASE PROMPT"


asyncio.run(picker_flow())
print("tui: bare /research opens the picker; esc leaves everything untouched  ✓")

print("MODES SMOKE OK — how we work together, chosen by you. amaze!")
