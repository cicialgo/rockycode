"""Free checks for experimental controls and budget accounting; no model/Docker."""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path
from decimal import Decimal

from bench.prompt_ablation import ARM_FACTORS, BASELINE_PATH, Meter, changed, schedule, sha, variants
from bench.prompt_cases import CASES
from rockycode.prompts.rocky import ROCKY_SYSTEM, ROCKY_SYSTEM_ZH

baseline = json.loads(BASELINE_PATH.read_text())
assert sha(baseline["en"]).startswith("2817209b")
assert sha(baseline["zh"]).startswith("28de148d")
for lang, shipped in (("en", ROCKY_SYSTEM), ("zh", ROCKY_SYSTEM_ZH)):
    arms = {k: v for k, v in variants(lang).items() if k in ARM_FACTORS}
    assert arms["baseline"] == baseline[lang]
    assert arms["adapted"] == shipped
    assert len(set(arms.values())) == 4
    parts = {k: v.split("\n\n", 3) for k, v in arms.items()}
    # Scope-only must not accidentally carry the changed completion contract.
    assert parts["scope_only"][2] == parts["baseline"][2]
    assert parts["completion_only"][1] == parts["baseline"][1]
    assert all(p[0] == parts["baseline"][0] and p[3] == parts["baseline"][3] for p in parts.values())

plan = schedule(CASES, ["en", "zh"], ARM_FACTORS, 2, 7)
assert len(plan) == 64
assert len({tuple(p.values()) for p in plan}) == 64
assert plan == schedule(CASES, ["en", "zh"], ARM_FACTORS, 2, 7)
assert changed({"a": "old", "b": "same"}, {"a": "new", "b": "same", "c": "added"}) == ["a", "c"]
assert changed({"a": "old"}, {}) == ["a"]

# A numeric acceptance check must accept Decimal as well as float; the task
# does not specify a numeric return class. Still reject wrong discount values.
app = types.ModuleType("app")
prior_app = sys.modules.get("app")
sys.modules["app"] = app
try:
    answers = {(): 0, (20, 30): 50, (99.99,): 99.99, (100,): 90,
               (60, 60): 108, (100.03,): 90.03}
    for convert in (float, lambda n: Decimal(str(n))):
        app.checkout_total = lambda prices: convert(answers[tuple(prices)])
        exec(CASES["clarify"]["acceptance"], {})
    app.checkout_total = lambda prices: sum(prices)
    try:
        exec(CASES["clarify"]["acceptance"], {})
    except AssertionError:
        pass
    else:
        raise AssertionError("the scorer accepted a missing discount")
finally:
    if prior_app is None:
        del sys.modules["app"]
    else:
        sys.modules["app"] = prior_app


class Stream:
    def __init__(self, with_usage=True):
        self.with_usage = with_usage
        self.closed = False

    async def __aiter__(self):
        usage = types.SimpleNamespace(model_dump=lambda: {"prompt_tokens": 80, "completion_tokens": 20})
        yield types.SimpleNamespace(usage=usage if self.with_usage else None, choices=[])

    async def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.calls = 0
        self.with_usage = True
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        return Stream(self.with_usage)


async def main():
    with tempfile.TemporaryDirectory() as td:
        client = Client()
        meter = Meter(client, Path(td), token_cap=50_000, call_cap=3, turn_cap=1)
        kwargs = {"model": "fake", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 128}
        stream = await meter.create(**kwargs)
        async for _ in stream:
            pass
        assert meter.tokens == 100 and client.calls == 1
        try:
            await meter.create(**kwargs)
        except RuntimeError as e:
            assert "per-turn" in str(e)
        else:
            raise AssertionError("turn cap failed")
        assert client.calls == 1, "a rejected request must not reach the client"
        meter.turn_calls = 0
        meter.token_cap = 101
        try:
            await meter.create(**kwargs)
        except RuntimeError as e:
            assert "budget" in str(e)
        else:
            raise AssertionError("token reserve failed")
        assert client.calls == 1
        unknown = Meter(client, Path(td), token_cap=50_000, call_cap=3, turn_cap=2)
        client.with_usage = False
        stream = await unknown.create(**kwargs)
        try:
            async for _ in stream:
                pass
        except RuntimeError as e:
            assert "without usage" in str(e)
        else:
            raise AssertionError("missing usage must fail the current turn")
        assert "without reliable usage" in unknown.stop_reason
        assert all("messages_sha256" in json.loads(line) for line in (Path(td) / "requests.jsonl").read_text().splitlines())


asyncio.run(main())
print("PROMPT ABLATION SMOKE OK — voice preserved; factors isolated; budgets enforced without API calls")
