"""A new model is one row: the request contract lives in the catalog row.

- The builtin rows are pinned here. Changing a builtin row's contract means
  changing this table too — that is the review step.
- A row the user adds in ~/.rockycode/models.toml gets its contract with no
  code change, in the main loop and in the small side calls.
- A row rocky cannot read is refused out loud (route error, /model, picker,
  host selection) — it never runs the generic contract in silence, and it
  never rides the home endpoint.

No network: every request lands in a recording transport.
"""
import asyncio
import json
import os
import re
import tempfile
import tomllib
import types
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

home = Path(tempfile.mkdtemp(prefix="rocky-switches-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens there
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_(SEARCH_)?MODEL", n)]:
    os.environ.pop(_name)
os.chdir(home)

from rockycode import onboarding  # noqa: E402

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain

from textual.widgets import OptionList, Static  # noqa: E402

from rockycode.engine import compaction, web  # noqa: E402
from rockycode.engine import providers as P  # noqa: E402
from rockycode.engine import route as R  # noqa: E402
from rockycode.engine.auxiliary import request_options  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.engine.model_protocol import GENERIC, SWITCHES, ModelProtocol  # noqa: E402
from rockycode.engine.tools import Tool  # noqa: E402
from rockycode.model_selection import select_model, text_completion  # noqa: E402

assert P.MODELS_TOML == home / "models.toml", P.MODELS_TOML

# ── the builtin rows, pinned ─────────────────────────────────────────────────
PINNED = {
    "deepseek-flash": dict(replay_reasoning="with_tools", reasoning_required=True, strict_finish=True,
                           sampling_thinking=(("top_p", 1.0),), sampling_plain=(("temperature", 1.0),)),
    "kimi-k3.1-pro": dict(replay_reasoning="always", strict_finish=True, output_limit="max_completion_tokens"),
    "minimax-m3.1-flash": dict(replay_reasoning="always", strict_finish=True),
    "qwen4-max": dict(replay_reasoning="always", strict_finish=True, output_limit="max_completion_tokens"),
    "mimo-v2.6-pro": dict(replay_reasoning="always", strict_finish=True, tool_choice_none=False,
                          output_limit="max_completion_tokens"),
}
bundled = tomllib.loads(P.BUILTIN_MODELS_TOML.read_text(encoding="utf-8"))
shipped = {mid: p.spec(mid) for p in P._providers_from_data(bundled).values() for mid in p.models}
assert set(shipped) == {mid for mid, row in bundled["models"].items() if not row.get("hidden")}, \
    "every shipped row loads"
assert set(PINNED) <= set(shipped), set(PINNED) - set(shipped)
for mid, spec in shipped.items():
    assert spec.protocol == ModelProtocol(**PINNED.get(mid, {})), \
        f"{mid}: the row's contract changed — review it and update PINNED ({spec.protocol.label or 'generic'})"
    assert set(bundled["models"][mid]) <= P.MODEL_FIELDS, mid
assert not P.refused_rows(), P.refused_rows()
assert set(SWITCHES) <= P.MODEL_FIELDS and GENERIC == ModelProtocol() and GENERIC.label == ""
print(f"builtin: {len(shipped)} rows load, {len(PINNED)} carry switches, each pinned to this table  ✓")


# ── a recording endpoint ─────────────────────────────────────────────────────
async def echo(value):
    return value


REGISTRY = {"echo": Tool("echo", {"type": "function", "function": {
    "name": "echo", "parameters": {"type": "object", "properties": {
        "value": {"type": "string"}}, "required": ["value"]}}}, echo, risk="safe")}


def stream(deltas, finish):
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    for c in chunks:
        c.update(id="fixture", object="chat.completion.chunk", created=0, model="fixture")
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")


class Wire:
    def __init__(self):
        self.bodies, self.script = [], []

    def client(self, **kw):
        return AsyncOpenAI(http_client=httpx.AsyncClient(
            trust_env=False, transport=httpx.MockTransport(self._respond)), **kw)

    def _respond(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json={
                "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "summary"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        return self.script.pop(0) if self.script else stream(
            [{"reasoning_content": "think"}, {"content": "done"}], "stop")


TOOL_CALL = [{"reasoning_content": "think-tool"},
             {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"value": "ok"}'}}]}]


def replayed(body) -> list:
    return [m.get("reasoning_content") for m in body["messages"] if m["role"] == "assistant"]


async def session(spec, *, tools=True, thinking=True):
    """One tool turn and one plain turn on *spec*; the engine and every request body."""
    wire = Wire()
    with patch("openai.AsyncOpenAI", side_effect=wire.client):
        engine = Engine(spec, workdir=home, registry=REGISTRY if tools else {}, thinking=thinking,
                        max_steps=3, finalize_steps=0)
        engine.evolve_enabled = False
        if tools:
            wire.script.append(stream(TOOL_CALL, "tool_calls"))
        for text in ("first", "second"):
            async for _ in engine.run_turn(text):
                pass
    return engine, wire


def overlay(text: str) -> None:
    P.MODELS_TOML.write_text(text, encoding="utf-8")


KEYS = {"ROCKYCODE_API_KEY": "k-home", "ROCKYCODE_KIMI_API_KEY": "k-kimi", "ROCKYCODE_MINIMAX_API_KEY": "k-minimax",
        "ROCKYCODE_MIMO_API_KEY": "k-mimo", "ROCKYCODE_QWEN_PLAN_API_KEY": "k-plan"}


async def builtin_requests():
    # Each pinned row, on the real SDK transport: what its switches put on the wire.
    for spec, cap, sampling_on, with_tools, without_tools in (
            ("deepseek-flash", "max_tokens", {"top_p": 1.0}, ["think-tool", "think"], [None]),
            ("kimi-k3.1-pro", "max_completion_tokens", {}, ["think-tool", "think"], ["think"]),
            ("minimax-m3.1-flash", "max_tokens", {}, ["think-tool", "think"], ["think"]),
            ("qwen-plan:qwen4-max", "max_completion_tokens", {}, ["think-tool", "think"], ["think"]),
            ("mimo-v2.6-pro", "max_completion_tokens", {}, ["think-tool", "think"], ["think"]),
            ("kimi-k3", "max_tokens", {}, [None, None], [None])):  # a row with no switches
        engine, wire = await session(spec)
        last = wire.bodies[-1]
        assert [k for k in last if k.startswith("max_")] == [cap], (spec, sorted(last))
        assert {k: last[k] for k in ("temperature", "top_p") if k in last} == sampling_on, (spec, last.keys())
        assert replayed(last) == with_tools, (spec, replayed(last))
        _, plain = await session(spec, tools=False)
        assert replayed(plain.bodies[-1]) == without_tools, (spec, replayed(plain.bodies[-1]))
        if spec == "deepseek-flash":
            _, off = await session(spec, thinking=False)
            assert off.bodies[-1]["temperature"] == 1.0 and "top_p" not in off.bodies[-1]
        # The summarize call: tool schemas ride along only where tool_choice="none" is honoured.
        before = len(wire.bodies)
        await compaction.summarize(
            engine.client, engine.wire_model, engine._api_messages(), [t.schema for t in REGISTRY.values()],
            reasoning=engine.reasoning_policy, efforts=engine.efforts, thinking_off=engine.thinking_off,
            protocol=engine.protocol)
        summary = wire.bodies[before]
        assert ("tools" in summary) == engine.protocol.tool_choice_none == (spec != "mimo-v2.6-pro"), spec
        assert [k for k in summary if k.startswith("max_")] == [cap], spec
        # The small side calls cap the output in the row's own field too (they sent
        # max_tokens to every model before).
        options = request_options(spec, 128)
        assert [k for k in options if k.startswith("max_")] == [cap], (spec, options)
        before = len(wire.bodies)
        await text_completion(engine.client, P.Choice(*P.resolve(spec)), [{"role": "user", "content": "x"}], 64)
        assert [k for k in wire.bodies[before] if k.startswith("max_")] == [cap], spec
    print("builtin: cap field, fixed sampling, reasoning replay and summarize shape follow each row  ✓")
    print("side calls: titles, goal review and bounded completions use the row's cap field  ✓")


async def one_row():
    # The whole integration of a new model: this row. No Python changes.
    overlay('''
[models."example-new-model"]
provider = "kimi"
wire = "Example-New-Model"
context = 262144
max_output = 32768
replay_reasoning = "always"
strict_finish = true
tool_choice_none = false
output_limit = "max_completion_tokens"
[models."example-new-model".sampling]
thinking = { temperature = 1.0 }
plain = { temperature = 0.6, top_p = 0.95 }

[models."kimi-k3"]
replay_reasoning = "with_tools"
''')
    assert not P.refused_rows(), P.refused_rows()
    engine, wire = await session("kimi:example-new-model")
    last = wire.bodies[-1]
    assert last["model"] == "Example-New-Model" and last["max_completion_tokens"] == 32768
    assert "max_tokens" not in last and last["temperature"] == 1.0 and "top_p" not in last
    assert replayed(last) == ["think-tool", "think"], "its reasoning comes back, as the row asks"
    assert engine.context_window == 262144 and engine.route.base_url == P.catalog()["kimi"].endpoints[0].base_url
    assert engine.protocol.label == ("replay_reasoning=always strict_finish tool_choice_none=false "
                                     "output_limit=max_completion_tokens sampling.thinking.temperature=1.0 "
                                     "sampling.plain.temperature=0.6 sampling.plain.top_p=0.95")
    assert engine._route()["protocol"] == engine.protocol.label, "records name the contract by its switches"
    _, off = await session("kimi:example-new-model", tools=False, thinking=False)
    assert (off.bodies[-1]["temperature"], off.bodies[-1]["top_p"]) == (0.6, 0.95)
    assert replayed(off.bodies[-1]) == ["think"], "always = also in a request without tools"
    options = request_options("example-new-model", 128)
    assert options["model"] == "Example-New-Model" and "max_completion_tokens" in options
    # A stream with no finish_reason is refused for this row; a generic row lets plain text through.
    wire = Wire()
    with patch("openai.AsyncOpenAI", side_effect=wire.client):
        strict = Engine("kimi:example-new-model", workdir=home, registry={})
        wire.script.append(stream([{"content": "cut"}], None))
        kinds = [type(ev).__name__ async for ev in strict.run_turn("hi")]
    assert "EngineError" in kinds and "TurnFinished" not in kinds, kinds
    # One switch on a builtin row is one line in the override.
    _, wire = await session("kimi-k3")
    assert replayed(wire.bodies[-1]) == ["think-tool", "think"]
    _, plain = await session("kimi-k3", tools=False)
    assert replayed(plain.bodies[-1]) == [None], "with_tools = only when the request carries tools"
    print("one row: a model added in ~/.rockycode/models.toml gets its whole contract from that row  ✓")
    print("one row: a builtin row takes a one-line switch override  ✓")


BAD = {
    "bad-typo": ('replay_reasonning = "always"', "unknown field `replay_reasonning` — did you mean `replay_reasoning`?"),
    "bad-guess": ("strict = true", "unknown field `strict`"),
    "bad-value": ('replay_reasoning = "sometimes"', "replay_reasoning must be one of never, with_tools, always"),
    "bad-flag": ('strict_finish = "yes"', "strict_finish must be true or false"),
    "bad-pair": ("reasoning_required = true", "reasoning_required needs replay_reasoning"),
    "bad-limit": ('output_limit = "max_output_tokens"', "output_limit must be one of max_tokens, max_completion_tokens"),
    "bad-sampling": ("sampling = { thinking = { top_k = 5 } }", "sampling.thinking takes numbers for temperature and top_p only"),
    "bad-old": ('protocol = "kimi_k31_pro"', "`protocol` is gone"),
    "bad-number": ('context = "big"', "a field has the wrong type"),
}


async def refusals():
    overlay("".join(f'[models."{mid}"]\nprovider = "kimi"\n{line}\n\n' for mid, (line, _) in BAD.items()) + '''
[models."deepseek-flash"]
strict_finsh = true

[models."not-reported-hidden"]
provider = "kimi"
hidden = true
bogus = 1

[models."not-reported-elsewhere"]
provider = "a-provider-rocky-does-not-have"
bogus = 1
''')
    rows = P.refused_rows()
    assert set(rows) == {*BAD, "deepseek-flash"}, set(rows)
    for mid, (_, why) in BAD.items():
        assert why in rows[mid], (mid, rows[mid])
    listed = {m for p in P.discover().values() for m in p.models}
    assert not listed & set(rows), "a refused row is not a model"
    assert "kimi-k3" in listed and "deepseek-v4-pro" in listed, "the rows beside it are untouched"
    for spec in ("kimi:bad-typo", "bad-typo", "Kimi:BAD-TYPO"):
        try:
            R.route(spec)
        except R.RouteError as error:
            message = str(error)
        else:
            raise AssertionError(f"{spec}: a refused row must not get a route — least of all the home one")
        assert "the row for 'bad-typo' was refused" in message and "did you mean `replay_reasoning`?" in message
        assert "models.toml" in message and "k-home" not in message
    # A builtin row broken by an override stops too — under its own id and under
    # a legacy id that now names it. It must not run on defaults.
    for spec in ("deepseek-flash", "deepseek-v4-flash", "deepseek:deepseek-v4-flash"):
        try:
            R.route(spec)
        except R.RouteError as error:
            assert "'deepseek-flash' was refused" in str(error) and "`strict_finish`" in str(error), str(error)
        else:
            raise AssertionError(f"{spec}: a refused home row must not run the generic contract")
    try:
        select_model("kimi:bad-value")
    except ValueError as error:
        assert "was refused: replay_reasoning must be one of" in str(error), str(error)
    else:
        raise AssertionError("a host cannot select a refused row")
    assert P.refused_hint("kimi-k3") is None and P.refused_hint("typo") is None, \
        "only the exact id: a refused row never captures a request for another unlisted model"
    assert R.route("typo").via == "home"
    assert any(h.startswith("bad-old — row refused · `protocol` is gone") for h in P.refused_hints())
    print(f"refused: {len(BAD)} kinds of bad row are named with their reason, never run, never sent home  ✓")

    # In the TUI: /model says why, and the picker keeps the row visible with its reason.
    from rockycode.tui.app import RockyCodeApp
    from rockycode.tui.modelpicker import ModelPicker
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace()))
    app = RockyCodeApp(Engine(model="deepseek-v4-pro", client=client, workdir=home), permission="yolo")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await app._handle_model("/model kimi:bad-typo")
        await pilot.pause()
        lines = "\n".join(str(w.render()) for w in app.query(Static))
        assert "the row for 'bad-typo' was refused" in lines and app.engine.model == "deepseek-v4-pro"
        await app._handle_model("/model")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        options = app.screen.query_one(OptionList)
        shown = "\n".join(str(options.get_option_at_index(i).prompt) for i in range(options.option_count))
        assert "bad-typo — row refused" in shown, shown
        app.screen.dismiss(None)
        await pilot.pause()
    print("refused: /model and the picker say which row and why  ✓")


def native_search():
    overlay("")
    with patch.dict(os.environ, {"ROCKYCODE_SEARCH_MODEL": "minimax:minimax-m3"}):
        try:
            web._native_route()
        except ValueError as error:
            assert "`search` role" in str(error), str(error)
        else:
            raise AssertionError("MiniMax search runs only on a row that carries the search role")
    with patch.dict(os.environ, {"ROCKYCODE_SEARCH_MODEL": "minimax:minimax-m3.1-flash"}):
        provider, model, key, url = web._native_route()
        assert (model, key) == ("minimax-m3.1-flash", "k-minimax") and url.endswith("/anthropic/v1/messages")
    assert P.role_model("search") == "deepseek-flash", "the default search model is unchanged"
    print("search: MiniMax's backend is gated by the row's role, not by a model name in code  ✓")


async def main():
    with patch.dict(os.environ, KEYS):
        await builtin_requests()
        await one_row()
        await refusals()
        native_search()


asyncio.run(main())

# Nothing in the package names a model's contract any more.
ROOT = Path(__file__).resolve().parents[1]
named = [f"{f.relative_to(ROOT)}:{n}" for f in sorted((ROOT / "rockycode").rglob("*.py"))
         for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
         if re.search(r"get_protocol|PROTOCOLS\b|\"(deepseek_flash|kimi_k31_pro|mimo_v26|qwen4_max_preview|minimax_m31_flash)\"", line)]
assert not named, named
print("audit: no code names a model's contract  ✓")

print("MODEL SWITCHES OK — a new model is one row. amaze!")
