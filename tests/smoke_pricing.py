"""Pricing + ledger smoke: dual-currency (the registry's real DeepSeek tables,
NOT a conversion), the weekday-aware peak-hour surcharge, legacy-id aliases,
~/.rockycode/pricing.toml override, and flash-search capture. Deterministic —
every test pins the request time."""
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rockycode.pricing import UsageLedger, load_pricing

OFFPEAK = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)    # Monday, after effective date, outside windows
PEAK = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)        # Monday, inside 06:00–10:00
WEEKEND = datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)     # Saturday, inside a window → still off-peak
PRE_EFFECT = datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)  # Wednesday in a window, before the mid-July start
FLASH_IN, FLASH_OUT = 0.15, 0.60   # deepseek-flash USD off-peak (V4.1, 2026-09-10 table)
_1M = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}


def test_dual_currency():
    led = UsageLedger()  # peak disabled by default
    led.add("deepseek-v4-pro", _1M, at=OFFPEAK)
    led.add("deepseek-flash", _1M, at=OFFPEAK)
    # USD from the USD table: pro 0.66+1.98, flash 0.15+0.60
    assert abs(led.cost("usd") - (0.66 + 1.98 + FLASH_IN + FLASH_OUT)) < 1e-9, led.cost("usd")
    # CNY from the CNY table — independent numbers, NOT usd*ratio: pro 4.5+13.5, flash 1+4
    assert abs(led.cost("cny") - (4.5 + 13.5 + 1.0 + 4.0)) < 1e-9, led.cost("cny")
    assert led.configured("usd") and led.configured("cny")
    print(f"dual-currency: ${led.cost('usd'):.3f} / ¥{led.cost('cny'):.1f} (independent tables)  ok")


def test_peak():
    led = UsageLedger()  # ships peak enabled (2x, windows 01–04 & 06–10 UTC, Mon–Fri, from mid-July)
    led.add("deepseek-flash", _1M, at=PEAK)
    assert abs(led.cost("usd") - (FLASH_IN + FLASH_OUT) * 2) < 1e-9, led.cost("usd")   # 2x in-window
    off = UsageLedger()
    off.add("deepseek-flash", _1M, at=OFFPEAK)
    assert abs(off.cost("usd") - (FLASH_IN + FLASH_OUT)) < 1e-9, off.cost("usd")       # off-peak base
    wk = UsageLedger()
    wk.add("deepseek-flash", _1M, at=WEEKEND)
    assert abs(wk.cost("usd") - (FLASH_IN + FLASH_OUT)) < 1e-9, wk.cost("usd")         # Saturday: no surcharge
    pre = UsageLedger()
    pre.add("deepseek-flash", _1M, at=PRE_EFFECT)
    assert abs(pre.cost("usd") - (FLASH_IN + FLASH_OUT)) < 1e-9, pre.cost("usd")       # in-window but before mid-July
    # a Beijing public holiday listed on the schedule bills off-peak too
    hol = UsageLedger(pricing={**load_pricing(), "peaks": {"deepseek": {**load_pricing()["peaks"]["deepseek"],
                                                                        "holidays": ["2026-08-03"]}}})
    hol.add("deepseek-flash", _1M, at=PEAK)
    assert abs(hol.cost("usd") - (FLASH_IN + FLASH_OUT)) < 1e-9, hol.cost("usd")
    print("peak-hour: 2x in-window on weekdays after mid-July; weekends/holidays/before = base  ok")


def test_legacy_alias():
    # deepseek-v4-flash / -vision-exp are retired ids DeepSeek serves as V4.1
    # Flash; the registry aliases them so an old config/trajectory still prices
    led = UsageLedger()
    assert led.priced("deepseek-v4-flash") and led.priced("deepseek-v4-flash-vision-exp")
    led.add("deepseek-v4-flash", _1M, at=OFFPEAK)
    assert abs(led.cost("usd") - (FLASH_IN + FLASH_OUT)) < 1e-9
    print("legacy ids price as deepseek-flash  ok")


def test_override():
    d = Path(tempfile.mkdtemp())
    p = d / "pricing.toml"
    p.write_text("[models.deepseek-flash.usd]\nout = 9.99\n")
    pricing = load_pricing(override_path=p)
    assert pricing["models"]["deepseek-flash"]["usd"]["out"] == 9.99            # overridden
    assert pricing["models"]["deepseek-flash"]["usd"]["in_miss"] == FLASH_IN  # untouched keeps default
    print("override: ~/.rockycode/pricing.toml merges over the built-in table  ok")


def test_cache_cheap():
    led = UsageLedger()
    led.add("deepseek-v4-pro",
            {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000, "completion_tokens": 0},
            at=OFFPEAK)
    assert led.cost("usd") < 0.03, led.cost("usd")  # 1M hit tokens at $0.022 vs $0.66 as misses
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
test_legacy_alias()
test_override()
test_cache_cheap()
asyncio.run(test_web_capture())
test_config()
print("PRICING SMOKE OK — dual-currency, peak-hour, override, flash capture. amaze!")
