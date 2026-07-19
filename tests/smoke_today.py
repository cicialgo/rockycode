"""Date grounding: rocky must know what day it is, cheaply.

- with_today: ONE date line at session build (prefix stays byte-identical
  turn to turn — no per-turn cache damage; bench never calls it).
- web results: stamped with the day they happened, so recency judgment is
  grounded at the point of use even when a session crosses midnight.
"""
import asyncio
from datetime import date, datetime

from rockycode.prompts.rocky import ROCKY_SYSTEM, with_today

today = date.today().isoformat()
weekday = f"{datetime.now():%A}"

out = with_today("You are Rocky.")
assert out == f"You are Rocky.\n\nToday is {today} ({weekday}).", out
assert with_today(ROCKY_SYSTEM).startswith(ROCKY_SYSTEM.rstrip()), \
    "the base prompt must stay a byte-identical prefix (cache!)"
assert ROCKY_SYSTEM.count("Today is") == 0, \
    "the date must NOT live in the base prompt — bench prompts stay date-free"
print("with_today: one dated line, base prompt untouched, bench-safe  ✓")

# web results carry the day they happened
from rockycode.engine import tools as tools_mod
from rockycode.engine import web


async def main():
    async def fake_native(q):
        return f"answer for {q}"

    async def fake_fetch(url):
        return "page text"

    reg = web.build_web_tools(search_order=("native",),
                              backends={"native": fake_native}, fetch_fn=fake_fetch)
    out, ok = await tools_mod.execute(reg, "web_search", '{"query": "codex update 2026"}')
    assert ok and out.startswith(f"[searched {today}] "), out[:60]
    out, ok = await tools_mod.execute(reg, "web_fetch", '{"url": "https://ex.com"}')
    assert ok and out.startswith(f"[fetched {today}] "), out[:60]
    print("web: results stamped with the search/fetch date  ✓")


asyncio.run(main())
print("TODAY SMOKE OK — rocky knows what day it is. amaze!")
