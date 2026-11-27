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
os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
# dead port — picker contents must not depend on the dev's own ollama
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"

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
        from textual.widgets import Input, OptionList
        from rockycode.tui.modelpicker import EndpointPicker, ModelPicker
        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "bare /model must open the picker"
        ol = app.screen.query_one(OptionList)
        joined = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "deepseek-flash" in joined, "deepseek always listed (flash default)"
        assert "❖ sees images" in joined, "flash sees — badge shows on the home key"
        assert "deepseek-v4-flash-vision-exp" not in joined, "the retired id is gone from the picker"
        assert "minimax-m3" not in joined, "unkeyed minimax must be hidden from short list"
        assert "openrouter-free" not in joined, "unkeyed openrouter must be hidden from short list"
        assert "more (no key yet)" in joined, "the 'N more' row must point at the hidden catalog"
        # the "N more" row reopens over the full catalog — MODEL first: one row
        # per model, plan/custom URLs folded behind it ("N URLs"), never eid:model rows
        app.screen.dismiss("all")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker), "'all' must reopen the picker on the catalog"
        ol = app.screen.query_one(OptionList)
        allj = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        for m in ("minimax-m3", "minimax-m3.1-flash", "kimi-k3", "glm-5.3", "glm-5.3-flash",
                  "qwen3.8-max", "mimo-v2.6-pro", "step-5-preview",
                  "openrouter-free"):
            assert m in allj, f"catalog shows {m}"
        assert allj.count("qwen3.8-max") == 1, "one row per MODEL — official/plan fold into step 2"
        assert "2 URLs" in allj, "a model with a plan endpoint advertises its URL step"
        # picking a multi-endpoint model opens step 2: which URL serves it
        qwen = [g for g in app.screen.groups if g[0].model == "qwen3.8-max"][0]
        app.screen.dismiss(qwen)
        await pilot.pause()
        assert isinstance(app.screen, EndpointPicker), "multi-URL model → endpoint step"
        ol = app.screen.query_one(OptionList)
        epj = "\n".join(str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count))
        assert "qwen " in epj and "qwen-plan" in epj and "Token Plan" in epj, epj
        assert "custom base URL" in epj, "own gateway/proxy row is always offered"
        app.screen.dismiss(None)  # esc path — no switch
        await pilot.pause()
        assert app.engine.provider_name == "deepseek", "cancel must not switch"
        print("model: model-first picker + endpoint step (official · plan · custom URL)  ✓")

        # switching to a provider whose rocky-key is missing → the key field opens
        # (tests/smoke_tui_key.py walks it); cancelling it leaves everything as it was
        from rockycode.tui.keyprompt import KeyScreen
        os.environ.pop("ROCKYCODE_MINIMAX_API_KEY", None)
        await app._handle_model("/model minimax")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, KeyScreen):
                break
        assert isinstance(app.screen, KeyScreen) and app.screen.key_env == "ROCKYCODE_MINIMAX_API_KEY"
        assert app.engine.provider_name == "deepseek", "no key → must not switch"
        await pilot.press("escape")
        for _ in range(40):
            await pilot.pause(0.05)
            if not isinstance(app.screen, KeyScreen):
                break
        await pilot.pause(0.1)
        allrender = "\n".join(str(w.render()) for w in app.query(Static))
        assert "no key saved" in allrender and "add or change a key" in allrender, "says where to add it later"
        assert app.engine.provider_name == "deepseek"
        print("model: missing rocky-owned key → asks for it right there; cancel = no switch  ✓")

        # with the key set, the switch takes: model + shape + spec limits, history intact
        os.environ["ROCKYCODE_MINIMAX_API_KEY"] = "sk-rocky-minimax"
        before = list(app.engine.history)
        await app._handle_model("/model minimax:m3")
        assert app.engine.provider_name == "minimax" and app.engine.model == "minimax-m3"
        assert app.engine.reasoning_policy == "minimax" and app.engine.cache_field == "cached_tokens"
        assert app.engine.max_tokens == 131_072, "output cap follows the model's registry entry"
        assert app.engine.history == before, "switch must not touch history"
        assert app.engine.route.key_env == "ROCKYCODE_MINIMAX_API_KEY" and "minimax" in app.engine.route.base_url, \
            "the engine knows the route it switched to (auth hints, explore children, /goal)"
        os.environ.pop("ROCKYCODE_MINIMAX_API_KEY", None)
        print("model: /model minimax:m3 switches provider+model+shape+limits live  ✓")

        # an OLD regional key name still unlocks the provider (alias)
        os.environ["ROCKYCODE_KIMI_CN_API_KEY"] = "sk-rocky-kimi-old"
        await app._handle_model("/model kimi")
        assert app.engine.provider_name == "kimi" and app.engine.model == "kimi-k3"
        assert app.engine.vision_enabled and not app.engine.thinking_off
        os.environ.pop("ROCKYCODE_KIMI_CN_API_KEY", None)
        print("model: legacy ROCKYCODE_KIMI_CN_API_KEY still works as an alias  ✓")

        # model-level vision on one deepseek key: flash sees, pro doesn't;
        # the legacy vision-exp id lands on flash
        os.environ.setdefault("ROCKYCODE_API_KEY", "sk-rocky-test")
        await app._handle_model("/model deepseek-v4-flash-vision-exp")
        assert app.engine.model == "deepseek-flash", "retired id → V4.1 Flash"
        assert app.engine.vision_enabled, "flash must enable image input"
        assert app.engine.max_tokens == 393_216 and app.engine.context_window == 1_048_576
        await app._handle_model("/model deepseek:pro")
        assert app.engine.model == "deepseek-v4-pro"
        assert not app.engine.vision_enabled, "pro is text-only"
        # the effort note is wire-honest per provider
        app.engine.reasoning_effort = "high"
        assert "sends" not in app._effort_note(), app._effort_note()
        os.environ["ROCKYCODE_STEPFUN_API_KEY"] = "sk-rocky-step"
        await app._handle_model("/model stepfun")
        assert "sends medium to stepfun" in app._effort_note(), app._effort_note()
        app.engine.thinking = False
        assert "can't switch thinking off" in app._effort_note(), app._effort_note()
        os.environ.pop("ROCKYCODE_STEPFUN_API_KEY", None)
        print("model: vision is per-MODEL on one key; effort note names the wire tier  ✓")

        # custom base URL stays editable on the SECOND open. The first pick
        # writes <provider>-custom; the next /model highlights that row, and
        # Enter must reopen the field pre-filled — not re-apply and close.
        from rockycode.engine import providers as P
        os.environ["ROCKYCODE_QWEN_API_KEY"] = "sk-rocky-qwen"
        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        joined = "\n".join(
            str(app.screen.query_one(OptionList).get_option_at_index(i).prompt)
            for i in range(app.screen.query_one(OptionList).option_count))
        assert "qwen3.8-max" in joined and "2 URLs" in joined, \
            "short list keeps the plan URL so the URL step is reachable without /model all"
        qwen_i = next(i for i, g in enumerate(app.screen.groups) if g[0].model == "qwen3.8-max")
        await _move_to(pilot, app.screen, str(qwen_i))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EndpointPicker), "keyed qwen (official + plan) still asks which URL"
        await _move_to(pilot, app.screen, "custom")
        await pilot.press("enter")
        await pilot.pause()
        inp = app.screen.query_one("#custom-url", Input)
        assert inp.display and inp.has_focus and inp.value == "", \
            "first custom pick: empty editable field"
        for ch in "https://proxy.example/v1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.engine.provider_name == "qwen-custom"
        assert str(app.engine.client.base_url).rstrip("/") == "https://proxy.example/v1"
        assert 'qwen = "https://proxy.example/v1"' in P.ENDPOINTS_TOML.read_text()

        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        qwen_i = next(i for i, g in enumerate(app.screen.groups) if g[0].model == "qwen3.8-max")
        await _move_to(pilot, app.screen, str(qwen_i))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EndpointPicker), "second open still reaches the URL step"
        ol = app.screen.query_one(OptionList)
        highlighted = ol.get_option_at_index(ol.highlighted)
        assert highlighted.id != "custom" and "qwen-custom" in str(highlighted.prompt), \
            "second open lands on the saved URL row"
        inp = app.screen.query_one("#custom-url", Input)
        assert inp.display and inp.value == "https://proxy.example/v1", \
            "saved URL is already in the field on the second open"
        assert not inp.has_focus, "list keeps focus so another URL can still be picked"
        # Enter on that highlighted row used to dismiss and re-apply. It must edit.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EndpointPicker), \
            "second open: Enter on the saved URL must not close the picker"
        inp = app.screen.query_one("#custom-url", Input)
        assert inp.has_focus and inp.value == "https://proxy.example/v1"
        await pilot.press("backspace", "backspace")
        await pilot.press("v", "2")
        await pilot.pause()
        assert inp.value == "https://proxy.example/v2", inp.value
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, EndpointPicker)
        assert app.engine.provider_name == "qwen-custom"
        assert str(app.engine.client.base_url).rstrip("/") == "https://proxy.example/v2"
        assert 'qwen = "https://proxy.example/v2"' in P.ENDPOINTS_TOML.read_text()
        os.environ.pop("ROCKYCODE_QWEN_API_KEY", None)
        print("model: custom URL stays editable the second time the picker opens  ✓")


def _option_index(screen, option_id: str) -> int:
    from textual.widgets import OptionList
    ol = screen.query_one(OptionList)
    for i in range(ol.option_count):
        if ol.get_option_at_index(i).id == option_id:
            return i
    raise AssertionError(f"no option {option_id}")


async def _move_to(pilot, screen, option_id: str) -> None:
    from textual.widgets import OptionList
    ol = screen.query_one(OptionList)
    target = _option_index(screen, option_id)
    cur = ol.highlighted or 0
    key = "down" if target >= cur else "up"
    for _ in range(abs(target - cur)):
        await pilot.press(key)
    await pilot.pause()


asyncio.run(main())
print("TUI MODEL SMOKE OK — one command, provider + exact model. amaze!")
