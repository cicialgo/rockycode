"""REAL DeepSeek API canary — pins the assumption every fake-stream smoke test
relies on: an assistant `tool_calls` message with NO matching tool reply is
rejected with a 400. If the API ever stops enforcing that, our fakes are lying
(smoke tests stay green while prod breaks), and this canary tells us.

Hits the network and spends a few tokens. Skips cleanly (exit 0) when creds are
absent, so it never blocks the free path. Local / pre-release only — kept OUT of
CI because the repo is MIT/public and the API key stays local.

    python tests/run_real.py            # runner, or directly:
    python tests/real_tool_contract.py

Needs ROCKYCODE_API_KEY, ROCKYCODE_BASE_URL, ROCKYCODE_MODEL (from ~/.rockycode/.env).
"""
import asyncio
import os
import sys

# Load creds the same way the CLI does (project .env, then rocky's global
# .env/keychain) so the canary "just works" wherever `rockycode` works.
try:
    from dotenv import load_dotenv

    load_dotenv()
    from rockycode.onboarding import bootstrap_credentials

    bootstrap_credentials()
except Exception:  # noqa: BLE001 — env may already be exported
    pass

from rockycode.onboarding import current_key

if not current_key() or not os.getenv("ROCKYCODE_MODEL"):
    print("SKIP real_tool_contract — set ROCKYCODE_API_KEY + ROCKYCODE_MODEL to run.")
    sys.exit(0)

from openai import AsyncOpenAI, BadRequestError

MODEL = os.environ["ROCKYCODE_MODEL"]


async def main() -> None:
    client = AsyncOpenAI(api_key=current_key(), max_retries=0, timeout=60.0)

    # 1) sanity: creds + endpoint actually work.
    r = await client.chat.completions.create(
        model=MODEL, max_tokens=5,
        messages=[{"role": "user", "content": "reply with the single word: ok"}],
    )
    assert r.choices, "no choices from a basic completion"
    print("· basic completion OK")

    # 2) THE canary: an assistant tool_calls message with no tool reply must 400.
    malformed = [
        {"role": "user", "content": "run a tool"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_x", "type": "function",
            "function": {"name": "noop", "arguments": "{}"},
        }]},
        {"role": "user", "content": "this user message never answered the tool_call"},
    ]
    try:
        await client.chat.completions.create(model=MODEL, max_tokens=5, messages=malformed)
    except BadRequestError as e:
        assert "tool" in str(e).lower(), f"got a 400 but not about tool_calls: {e}"
        print("· canary OK — API still rejects unanswered tool_calls (400)")
        print("REAL CONTRACT OK — the invariant our fakes assume still holds. amaze!")
        return
    raise AssertionError(
        "EXPECTED a 400 for an unanswered tool_call, but the API accepted it. The "
        "assumption behind smoke_interrupt and the loop.py backfill may no longer "
        "hold — re-examine the cancel/deny paths."
    )


asyncio.run(main())
