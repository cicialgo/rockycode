"""Scrub secrets from tool output before it enters the conversation history.

History is the single thing that is BOTH sent to the model provider (the API
prompt) AND written to the trajectory log, so redacting tool output at the
tools.execute() chokepoint fixes both at once — and the model itself only ever
sees `[redacted]`, so it can't later echo a key it read. (The user's own API key
still travels in the request AUTH HEADER — that is how authentication works and
is untouched; what we scrub is secret text that shows up in message CONTENT,
e.g. from `env`, `cat .env`, or a printed token.)

Two passes:
  1. Known values — the literal values of the user's own sensitive env vars
     (OPENAI_API_KEY, ANTHROPIC_AUTH_TOKEN, *_TOKEN, …). Highest precision:
     redacts the actual secret wherever it appears (catches `env`/`printenv`).
  2. Shapes — regexes for well-known secret formats when we don't know the
     value (sk-…, ghp_…, AKIA…, AIza… google keys, JWTs, PRIVATE KEY blocks,
     Bearer …, KEY=value) plus a generic high-entropy 32–64-char token
     heuristic (mixed lower+UPPER+digit, so git SHAs / md5s are spared).
"""
from __future__ import annotations

import os
import re

# Env var NAMES considered secret; their VALUES are scrubbed from output.
_SENSITIVE_ENV = re.compile(r"(API_KEY|_TOKEN|_SECRET|PASSWORD|PASSWD|ANTHROPIC_|OPENAI_)", re.I)
# Don't redact trivially short values — avoids nuking "1"/"true"/"dev" if such a
# value ever lands in a sensitive-named var.
_MIN_VALUE_LEN = 6

_SHAPES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[redacted: private key]"),
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted: api key]"),
    (re.compile(r"\bgh[posur]_[A-Za-z0-9]{20,}"), "[redacted: github token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted: aws key id]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted: slack token]"),
    # Google API key: literal "AIza" + 35 url-safe base64 chars (fixed length).
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted: google api key]"),
    # JWT: three base64url segments separated by dots, first starts with the
    # canonical `eyJ` ({"…} header). Matches access/id tokens people print.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
     "[redacted: jwt]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"), "Bearer [redacted]"),
    # NAME=value / NAME: value where the name ends in a secret word. The word
    # must sit immediately before the separator, so MAX_TOKENS / TOKEN_COUNT
    # (no separator right after the word) are NOT matched; ACCESS_TOKEN= is.
    (re.compile(r"""(?im)^(\s*[\w.-]*(?:_API_KEY|_TOKEN|_SECRET|SECRET_KEY|PASSWORD|PASSWD)\s*[=:]\s*)
                    (["']?[^\s"']{8,}["']?)""", re.VERBOSE),
     r"\1[redacted]"),
    # Generic high-entropy token: a 32–64 char base62-ish run that mixes
    # lower + UPPER + digit. The three-class requirement is what spares
    # low-diversity strings that are NOT secrets — a 40-hex git SHA / md5 (no
    # uppercase), an ALL-CAPS constant, a decimal id — while still catching the
    # opaque provider keys that don't carry a recognizable prefix. Runs LAST so
    # the labelled shapes above win. Best-effort; may occasionally over-redact a
    # mixed-case blob, which on tool output is the safe direction.
    (re.compile(r"\b(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)"
                r"[A-Za-z0-9_-]{32,64}\b"),
     "[redacted: token]"),
]


def _known_values() -> list[str]:
    vals = {
        v for k, v in os.environ.items()
        if v and len(v) >= _MIN_VALUE_LEN and _SENSITIVE_ENV.search(k)
    }
    # Longest first: if one secret value is a substring of another, redact the
    # bigger match before the smaller can partially hit it.
    return sorted(vals, key=len, reverse=True)


def redact(text: str) -> str:
    """Return *text* with known secret values and secret-shaped tokens masked."""
    if not text:
        return text
    for value in _known_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    for rx, repl in _SHAPES:
        text = rx.sub(repl, text)
    return text
