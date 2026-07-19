"""Context compaction: keep long sessions inside the model's window.

Two stages, cheapest first:

1. prune — deterministic, free. Old tool outputs (the bulk of any agent
   context) are stubbed down to a short head; the assistant's own messages
   and tool_calls survive, so the model still sees *what* it did, just not
   every byte it once read.
2. summarize — the recurrent state. If pruning can't get under the limit,
   one non-streaming API call folds everything except a recent tail into a
   dense state document, and history is rebuilt as [system, state, tail].
   The call sends the *unmodified* history plus one instruction message, so
   DeepSeek's automatic prefix cache makes its input nearly free.

Token math is deliberately conservative (3 chars ≈ 1 token): compacting a
little early costs a few cache misses; compacting late kills the turn.
"""
from __future__ import annotations

CHARS_PER_TOKEN = 3
MSG_OVERHEAD_TOKENS = 8

KEEP_RECENT_TOKENS = 8_000     # protected tail budget (estimated tokens)
PRUNE_KEEP_CHARS = 200         # head kept when a tool output is stubbed
PRUNE_STUB = "… [old output dropped during compaction — re-run the tool if needed]"
SUMMARY_MAX_TOKENS = 4096

SUMMARIZE_INSTRUCTION = """\
Context is nearly full. Before continuing, write a compact state document
for this session so far; older messages will be dropped and replaced by it.

Cover, with concrete file paths, names, and line references:
1. Task — what we are trying to accomplish, in one or two sentences.
2. State — what has been done and learned so far (files read/edited, key
   findings, commands run and their results).
3. Failures — approaches that did not work, so they are not retried.
4. Next — the immediate plan.

Write only the state document, no preamble. Be dense; facts over prose.
"""


def estimate_msg_tokens(msg: dict) -> int:
    chars = len(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return MSG_OVERHEAD_TOKENS + chars // CHARS_PER_TOKEN


def estimate_tokens(messages: list[dict]) -> int:
    return sum(estimate_msg_tokens(m) for m in messages)


def tail_start(history: list[dict], budget_tokens: int) -> int:
    """Index where the protected recent tail begins.

    Never 0 (the system prompt is handled separately), always compresses at
    least the older half of the conversation, and never starts on a `tool`
    message — a tool result without its assistant tool_calls parent is an
    invalid conversation, so the tail grows backward to include the parent.
    """
    i = len(history)
    spent = 0
    while i > 1:
        cost = estimate_msg_tokens(history[i - 1])
        if spent and spent + cost > budget_tokens:
            break
        spent += cost
        i -= 1
    floor = 1 + (len(history) - 1) // 2
    if i < floor:
        i = floor
    while 1 < i < len(history) and history[i]["role"] == "tool":
        i -= 1
    return i


def _prune_gain_chars(content: str) -> int:
    return len(content) - PRUNE_KEEP_CHARS - len(PRUNE_STUB) - 1


def prune_savings(history: list[dict], protect_from: int) -> int:
    """Estimated tokens that prune_tool_outputs would free. No mutation."""
    saved = 0
    for msg in history[1:protect_from]:
        if msg.get("role") == "tool":
            gain = _prune_gain_chars(msg.get("content") or "")
            if gain > 0:
                saved += gain // CHARS_PER_TOKEN
    return saved


def prune_tool_outputs(history: list[dict], protect_from: int) -> int:
    """Stub bulky tool outputs in history[1:protect_from], in place.

    Returns the number of messages stubbed. Deterministic given the same
    history and protect_from, so a trajectory's compaction record is enough
    to replay it.
    """
    n = 0
    for msg in history[1:protect_from]:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        if _prune_gain_chars(content) <= 0:
            continue
        msg["content"] = content[:PRUNE_KEEP_CHARS] + "\n" + PRUNE_STUB
        n += 1
    return n


MAX_MSG_CHARS = 40_000  # ~13k tokens; a single message above this is truncated
                        # head+tail, so one giant paste / tool result can't defeat
                        # compaction or overflow the window.


def truncate_oversized(history: list[dict], max_chars: int = MAX_MSG_CHARS) -> int:
    """Truncate any single message whose text content exceeds *max_chars* to a
    head + tail with an elision marker, in place. Skips the system prompt.
    Returns the count truncated.

    The backstop for what prune and summarize miss: prune only shrinks `tool`
    messages and summarize keeps the recent tail verbatim, so an oversized
    NON-tool message (e.g. a pasted 300k-char log that tail_start always keeps)
    survives both and keeps every step over the limit. This bounds it.
    """
    n = 0
    keep = max_chars // 2 - 60
    for msg in history[1:]:  # never the system prompt
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= max_chars:
            continue
        elided = len(content) - 2 * keep
        msg["content"] = (
            content[:keep]
            + f"\n… [{elided:,} chars elided to fit the context window] …\n"
            + content[-keep:]
        )
        n += 1
    return n


async def summarize(client, model: str, history: list[dict], tools: list[dict]) -> tuple[str, dict]:
    """One non-streaming call: history + instruction → state document.

    `tools` is passed through (with tool_choice="none") so the request body
    matches the main loop's shape and the prefix cache can hit; thinking is
    off — a summary needs no chain of thought.
    """
    resp = await client.chat.completions.create(
        model=model,
        messages=history + [{"role": "user", "content": SUMMARIZE_INSTRUCTION}],
        tools=tools,
        tool_choice="none",
        max_tokens=SUMMARY_MAX_TOKENS,
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    summary = (resp.choices[0].message.content or "").strip()
    usage: dict = {}
    if resp.usage is not None:
        try:
            u = resp.usage.model_dump()
        except AttributeError:
            u = dict(resp.usage)
        usage = {k: v for k, v in u.items() if isinstance(v, int)}
    return summary, usage


def state_message(summary: str) -> dict:
    return {
        "role": "user",
        "content": (
            "[context compacted] Older messages were compressed into this "
            "state document:\n\n"
            f"{summary}\n\n"
            "Continue the task from this state. The most recent messages "
            "follow unmodified — do not redo work recorded as done above."
        ),
    }
