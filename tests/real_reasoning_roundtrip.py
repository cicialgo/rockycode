#!/usr/bin/env python
"""REAL-API check (network, token-spending, skips without creds):

Does DeepSeek thinking mode require `reasoning_content` to be passed BACK on
an assistant turn that carries tool_calls? Two sources disagree:

  - api-docs reasoning_model guide (R1-era): reasoning_content in ANY input
    message → 400; function calling unsupported on deepseek-reasoner.
  - DeepSeek-Reasonix source (openai.go:399): V4 thinking mode "400s a
    tool_calls turn whose reasoning_content was dropped on a cache-miss
    replay" → they round-trip it on exactly those turns.

rockycode today NEVER stores reasoning_content (loop.py). This canary sends:
  A. tool-call turn WITHOUT reasoning_content  (rockycode-style history)
  B. tool-call turn WITH reasoning_content     (Reasonix-style history)
  C. PLAIN assistant turn WITH reasoning_content (docs say 400)
and prints which the live API accepts, so the loop's strategy is checked
against reality, not docs archaeology.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path.home() / ".rockycode" / ".env")
from rockycode.onboarding import load_credentials_into_env  # noqa: E402

load_credentials_into_env()

MODEL = (os.getenv("ROCKYCODE_MODEL") or "").strip()
if not MODEL or not (os.getenv("OPENAI_API_KEY") or "").strip():
    print("skip: OPENAI_API_KEY / ROCKYCODE_MODEL not set")
    sys.exit(0)

from openai import BadRequestError, OpenAI  # noqa: E402

client = OpenAI()  # base_url/key from env, same as the engine
THINK = {"thinking": {"type": "enabled"}}
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]
ASK = {"role": "user",
       "content": "Use the get_weather tool to check Hangzhou. Do not answer without the tool."}

r1 = client.chat.completions.create(
    model=MODEL, messages=[ASK], tools=TOOLS, extra_body=THINK, max_tokens=8192)
msg = r1.choices[0].message
if not msg.tool_calls:
    print("skip: model made no tool call (nothing to test)")
    sys.exit(0)
reasoning = getattr(msg, "reasoning_content", None) or ""
print(f"provoked tool call · reasoning_content on the turn: "
      f"{'present, ' + str(len(reasoning)) + ' chars' if reasoning else 'ABSENT'}")

tc = msg.tool_calls[0]
assistant = {
    "role": "assistant",
    "content": msg.content or "",
    "tool_calls": [{"id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}],
}
tool_result = {"role": "tool", "tool_call_id": tc.id, "content": "sunny, 31°C"}


def attempt(label: str, asst: dict) -> str:
    try:
        r = client.chat.completions.create(
            model=MODEL, messages=[ASK, asst, tool_result],
            tools=TOOLS, extra_body=THINK, max_tokens=8192)
        print(f"  {label}: ACCEPTED — {(r.choices[0].message.content or '')[:60]!r}")
        return "ok"
    except BadRequestError as e:
        print(f"  {label}: 400 — {str(e)[:160]}")
        return "400"


print("tool-call turn round-trips:")
a = attempt("A without reasoning_content (rockycode today)", dict(assistant))
with_rc = dict(assistant)
with_rc["reasoning_content"] = reasoning
b = attempt("B with reasoning_content    (Reasonix style) ", with_rc)

print("plain-turn passback (docs say 400):")
try:
    r3 = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "Say hi in three words."}],
        extra_body=THINK, max_tokens=4096)
    m3 = r3.choices[0].message
    client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": "Say hi in three words."},
            {"role": "assistant", "content": m3.content or "",
             "reasoning_content": getattr(m3, "reasoning_content", "") or ""},
            {"role": "user", "content": "And in two."},
        ],
        extra_body=THINK, max_tokens=4096)
    print("  C plain turn WITH reasoning_content: ACCEPTED (doc rule not enforced here)")
    c = "ok"
except BadRequestError as e:
    print(f"  C plain turn WITH reasoning_content: 400 — {str(e)[:160]}")
    c = "400"

print("\nverdict:")
if a == "ok":
    print("  ✓ rockycode's strip-everything history is ACCEPTED on tool-call turns —")
    print("    the Reasonix requirement does not reproduce here; no loop change needed.")
else:
    print("  ✗ rockycode's history is REJECTED on tool-call turns — the loop must")
    print("    store + round-trip reasoning_content on assistant turns with tool_calls.")
print(f"  (B with rc: {b} · C plain with rc: {c})")
sys.exit(0 if a == "ok" else 1)
