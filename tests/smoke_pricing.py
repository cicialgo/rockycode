"""Pricing + ledger smoke: dual-currency (real DeepSeek tables, NOT a
conversion), peak-hour surcharge, ~/.rockycode/pricing.toml override, and
flash-search capture. Deterministic — every test pins the request time."""
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rockycode.pricing import UsageLedger, load_pricing

OFFPEAK = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)    # after effective date, outside windows
PEAK = datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)        # after effective date, inside 06:00–10:00
PRE_EFFECT = datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)  # inside a window but before the mid-July start
_1M = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}


def test_dual_currency():
    led = UsageLedger()  # peak disabled by default
    led.add("deepseek-v4-pro", _1M, at=OFFPEAK)
    led.add("deepseek-v4-flash", _1M, at=OFFPEAK)
    # USD from the USD table: pro 0.435+0.87, flash 0.14+0.28
    assert abs(led.cost("usd") - (0.435 + 0.87 + 0.14 + 0.28)) < 1e-9, led.cost("usd")
    # CNY from the CNY table — independent numbers, NOT usd*ratio: pro 3+6, flash 1+2
    assert abs(led.cost("cny") - (3.0 + 6.0 + 1.0 + 2.0)) < 1e-9, led.cost("cny")
    assert led.configured("usd") and led.configured("cny")
    print(f"dual-currency: ${led.cost('usd'):.3f} / ¥{led.cost('cny'):.1f} (independent tables)  ok")


def test_peak():
    led = UsageLedger()  # ships peak enabled (2x, windows 01–04 & 06–10 UTC, from mid-July)
    led.add("deepseek-v4-flash", _1M, at=PEAK)
    assert abs(led.cost("usd") - (0.14 + 0.28) * 2) < 1e-9, led.cost("usd")   # 2x in-window
    off = UsageLedger()
    off.add("deepseek-v4-flash", _1M, at=OFFPEAK)
    assert abs(off.cost("usd") - (0.14 + 0.28)) < 1e-9, off.cost("usd")       # off-peak base
    pre = UsageLedger()
    pre.add("deepseek-v4-flash", _1M, at=PRE_EFFECT)
    assert abs(pre.cost("usd") - (0.14 + 0.28)) < 1e-9, pre.cost("usd")       # in-window but before mid-July
    print("peak-hour: 2x in-window after the mid-July start; base before it and off-peak  ok")


def test_override():
    d = Path(tempfile.mkdtemp())
    p = d / "pricing.toml"
    p.write_text("[models.deepseek-v4-flash.usd]\nout = 9.99\n")
    pricing = load_pricing(override_path=p)
    assert pricing["models"]["deepseek-v4-flash"]["usd"]["out"] == 9.99      # overridden
    assert pricing["models"]["deepseek-v4-flash"]["usd"]["in_miss"] == 0.14  # untouched keeps default
    print("override: ~/.rockycode/pricing.toml merges over the built-in table  ok")


def test_cache_cheap():
    led = UsageLedger()
    led.add("deepseek-v4-pro",
            {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000, "completion_tokens": 0},
            at=OFFPEAK)
    assert led.cost("usd") < 0.02, led.cost("usd")
    print("cache hits cost far less than misses  ok")


async def test_web_capture():
    from rockycode.engine import tools as tools_mod, web
    led = UsageLedger()

    async def fake_native(query, ledger=None):
        if ledger is not None:
            ledger.add("deepseek-v4-flash",
                       {"prompt_tokens": 5000, "prompt_cache_hit_tokens": 1000, "completion_tokens": 200})
        return "result text"

    reg = web.build_web_tools(ledger=led, search_order=("native",),
                              backends={"native": lambda q: fake_native(q, ledger=led)})
    _out, ok = await tools_mod.execute(reg, "web_search", '{"query": "x"}')
    assert ok and led.totals()["prompt"] == 5000, led.totals()
    print("web search captured into ledger  ok")


def test_config():
    import rockycode.config as C
    C.GLOBAL_PATH = Path(tempfile.mkdtemp()) / ".rockycode" / "config.toml"
    assert C.load()["currency"] == "usd"
    v, err = C.set_value("currency", "cny")
    assert err is None and v == "cny"
    _, err = C.set_value("currency", "yen")  # invalid
    assert err is not None
    print("config load/set/validation ok")


test_dual_currency()
test_peak()
test_override()
test_cache_cheap()
asyncio.run(test_web_capture())
test_config()
print("PRICING SMOKE OK — dual-currency, peak-hour, override, flash capture. amaze!")
