"""Session titles: one tiny flash call, fired-and-forgotten after the first
exchange, so the resume picker and exit card show "credential namespace fix"
instead of the first message echoed back.

Best-effort by design: offline, no key, a slow provider, or a weird reply all
mean "no title" — the first-message summary remains the fallback everywhere,
and nothing here may ever surface an error into the chat.
"""
from __future__ import annotations

import os

_MAX_CHARS = 60


def _title_model() -> str:
    return os.getenv("ROCKYCODE_TITLE_MODEL", "deepseek-v4-flash")


def _clean(raw: str) -> str | None:
    """First line, quotes/periods stripped, hard cap — a title, not a reply."""
    t = raw.strip().splitlines()[0].strip().strip('"\'“”「」').rstrip(".。").strip()
    if not t:
        return None
    return t[:_MAX_CHARS]


async def generate_title(client, first_user: str, first_reply: str) -> str | None:
    """3-8 word session title from the opening exchange, or None."""
    try:
        r = await client.chat.completions.create(
            model=_title_model(),
            max_tokens=24,
            messages=[
                {"role": "system", "content": (
                    "Write a 3-8 word title for this coding-chat session, in the "
                    "user's language. Output ONLY the title — no quotes, no period."
                )},
                {"role": "user", "content": (
                    f"[user]\n{first_user[:1000]}\n\n[assistant]\n{first_reply[:1000]}"
                )},
            ],
        )
        return _clean(r.choices[0].message.content or "")
    except Exception:  # noqa: BLE001 — titles are decoration; never break a session
        return None
