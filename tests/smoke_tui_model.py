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
        from rockycode.tui.modelpicker import EndpointPicker, ModelPicker
        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "bare /model must open the picker"
        ol = app.screen.query_one(OptionList)
        joined = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "deepseek-v4-flash" in joined, "deepseek always listed (flash default)"
        assert "❖ sees images" in joined, "flash-vision-exp rides the deepseek key — badge shows"
        assert "minimax-m3" not in joined, "unkeyed minimax must be hidden from short list"
        assert "more (no key yet)" in joined, "the 'N more' row must point at the hidden catalog"
        # the "N more" row reopens over the full catalog — MODEL first: one row
        # per model, regions folded behind it ("N URLs"), never eid:model rows
        app.screen.dismiss("all")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "'all' must reopen the picker on the catalog"
        ol = app.screen.query_one(OptionList)
        allj = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "minimax-m3" in allj and "kimi-k3" in allj and "glm-5.2" in allj, "catalog shows all models"
        assert allj.count("kimi-k3") == 1, "one row per MODEL — cn/en fold into step 2"
        assert "2 URLs" in allj, "a multi-endpoint model advertises its URL step"
        # picking a multi-endpoint model opens step 2: which URL serves it
        kimi = [g for g in app.screen.groups if g[0].model == "kimi-k3"][0]
        app.screen.dismiss(kimi)
        await pilot.pause()
        assert isinstance(app.screen, EndpointPicker), "multi-URL model → endpoint step"
        ol = app.screen.query_one(OptionList)
        epj = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "kimi-cn" in epj and "kimi-en" in epj, "endpoint step lists the regions"
        assert "custom base URL" in epj, "own gateway/proxy row is always offered"
        app.screen.dismiss(None)  # esc path — no switch
        await pilot.pause()
        assert app.engine.provider_name == "deepseek", "cancel must not switch"
        print("model: model-first picker + endpoint step (regions + custom URL)  ✓")

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

        # model-level vision: vision-exp flips the engine's vision flag ON,
        # plain flash flips it back OFF — same provider, same key, per-model
        os.environ.setdefault("ROCKYCODE_API_KEY", "sk-rocky-test")
        await app._handle_model("/model deepseek-v4-flash-vision-exp")
        assert app.engine.model == "deepseek-v4-flash-vision-exp"
        assert app.engine.vision_enabled, "vision-exp must enable image input"
        await app._handle_model("/model deepseek:flash")
        assert app.engine.model == "deepseek-v4-flash", "base model wins the 'flash' substring"
        assert not app.engine.vision_enabled, "plain flash is text-only"
        print("model: vision is per-MODEL — vision-exp on, flash off, one deepseek key  ✓")


asyncio.run(main())
print("TUI MODEL SMOKE OK — one command, provider + exact model. amaze!")
