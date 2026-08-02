"""/model in the TUI: bare lists provider:model options; a spec switches live.

Headless pilot, fake engine. The switch's client build is real (AsyncOpenAI
constructs without a network call), gated on a rocky-owned key we set here.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockymodel-"))

from textual.widgets import Static

from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp


def build_app():
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    eng = Engine(model="deepseek-v4-pro", client=client, workdir=Path.cwd())
    return RockyCodeApp(eng, permission="yolo")


async def main():
    app = build_app()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()

        # bare /model opens the PICKER over KEYED options only — deepseek
        # always, an "N more" row; unkeyed minimax hidden until the catalog
        for v in list(os.environ):
            if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY") and v != "ROCKYCODE_API_KEY":
                os.environ.pop(v, None)
        from textual.widgets import OptionList
        from rockycode.tui.modelpicker import ModelPicker
        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "bare /model must open the picker"
        ol = app.screen.query_one(OptionList)
        joined = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "deepseek:deepseek-v4-flash" in joined, "deepseek always listed (flash default)"
        assert "minimax-en:minimax-m3" not in joined, "unkeyed minimax must be hidden from short list"
        assert "more (no key yet)" in joined, "the 'N more' row must point at the hidden catalog"
        # the "N more" row reopens over the full EN/CN catalog incl. regions
        app.screen.dismiss("all")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "'all' must reopen the picker on the catalog"
        ol = app.screen.query_one(OptionList)
        allj = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "minimax-en:minimax-m3" in allj and "kimi-cn:" in allj and "zai:" in allj, "catalog shows regions"
        app.screen.dismiss(None)  # esc path — no switch
        await pilot.pause()
        assert app.engine.provider_name == "deepseek", "cancel must not switch"
        print("model: picker (keyed only, ↑↓/click) + 'N more' → full EN/CN catalog  ✓")

        # switching to a provider whose rocky-key is missing → actionable note, no switch
        os.environ.pop("ROCKYCODE_MINIMAX_EN_API_KEY", None)
        await app._handle_model("/model minimax-en")
        assert app.engine.provider_name == "deepseek", "no key → must not switch"
        allrender = "\n".join(str(w.render()) for w in app.query(Static))
        assert "ROCKYCODE_MINIMAX_EN_API_KEY" in allrender, "must name the var to set"
        print("model: missing rocky-owned key → says which var to set, no switch  ✓")

        # with the key set, the switch takes: model + policy change, history intact
        os.environ["ROCKYCODE_MINIMAX_EN_API_KEY"] = "sk-rocky-minimax"
        before = list(app.engine.history)
        await app._handle_model("/model minimax-en:m3")
        assert app.engine.provider_name == "minimax-en" and app.engine.model == "minimax-m3"
        assert app.engine.reasoning_policy == "openai"
        assert app.engine.history == before, "switch must not touch history"
        os.environ.pop("ROCKYCODE_MINIMAX_EN_API_KEY", None)
        print("model: /model minimax:m3 switches provider+model+policy live  ✓")


asyncio.run(main())
print("TUI MODEL SMOKE OK — one command, provider + exact model. amaze!")
