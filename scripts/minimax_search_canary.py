"""Opt-in, bounded real MiniMax search probe; prints no keys or thinking blocks."""
import argparse
import asyncio
import os

from rockycode.engine.web import _native, _native_route


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="allow real API billing (up to three HTTP requests)")
    args = parser.parse_args()
    if not args.run:
        parser.error("real API calls require --run; configure your own MiniMax endpoint/key first")
    os.environ.setdefault("ROCKYCODE_SEARCH_MODEL", "minimax:minimax-m3.1-flash")
    provider, _, _, _ = _native_route()
    if provider.search != "minimax_messages":
        raise SystemExit("select a MiniMax M3.1 Flash endpoint; refusing another provider")
    answer = await _native("Search MiniMax official server tools documentation. Which tool is available and which APIs support it? Cite official documentation URLs.")
    if not answer.strip() or "https://" not in answer:
        raise SystemExit("FAIL: empty or uncited answer; inspect provider capability before adoption")
    print(answer)
    print("Transport canary complete. Manually verify the cited official page and factual answer; this is not a search-quality benchmark.")


if __name__ == "__main__":
    asyncio.run(main())
