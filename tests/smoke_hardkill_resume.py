"""Regression: a hard-killed session must survive --resume.

The engine's finally-block repairs history + trajectory stubs when a turn is
*cancelled* (new submit / Esc), but a process that dies hard mid-tool (SIGKILL,
crash, OOM, laptop lid) never runs it — the trajectory then ends with an
assistant tool_calls message and NO tool response. Reloading that verbatim 400s
the first resumed request:

    400 — An assistant message with 'tool_calls' must be followed by tool
    messages responding to each 'tool_call_id'.
    (insufficient tool messages following tool_calls message)

`session.load_history` must self-heal exactly like the live engine's in-memory
`_repair_history`: stub the orphaned calls in-position, idempotently, and for
every orphan (not just the last). Fake trajectory, no API — deterministic,
free, safe for every-commit CI.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(tempfile.mkdtemp(prefix="rockyhardkill-"))

from rockycode.session import load_history, repair_history
from invariants import assert_history_api_valid


def write_trajectory(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")


def main():
    traj = Path("hardkilled.jsonl")
    write_trajectory(traj, [
        {"t": 1, "kind": "meta", "data": {"model": "fake"}},
        {"t": 2, "kind": "message", "data": {"role": "system", "content": "sys"}},
        {"t": 3, "kind": "message", "data": {"role": "user", "content": "do it"}},
        # answered tool round — must be left untouched
        {"t": 4, "kind": "message", "data": {"role": "assistant", "content": None,
                                            "tool_calls": [{"id": "call_ok", "type": "function",
                                                            "function": {"name": "bash", "arguments": "{}"}}]}},
        {"t": 5, "kind": "message", "data": {"role": "tool", "tool_call_id": "call_ok", "content": "done"}},
        {"t": 6, "kind": "message", "data": {"role": "assistant", "content": "nice"}},
        # hard kill mid-tool: two calls, zero responses, nothing after
        {"t": 7, "kind": "message", "data": {"role": "assistant", "content": None,
                                            "tool_calls": [
                                                {"id": "call_a", "type": "function",
                                                 "function": {"name": "bash", "arguments": "{}"}},
                                                {"id": "call_b", "type": "function",
                                                 "function": {"name": "grep", "arguments": "{}"}},
                                            ]}},
        # a mid-history orphan too: user interleaved, stub must land in-position
        {"t": 8, "kind": "message", "data": {"role": "assistant", "content": None,
                                            "tool_calls": [{"id": "call_c", "type": "function",
                                                            "function": {"name": "bash", "arguments": "{}"}}]}},
        {"t": 9, "kind": "message", "data": {"role": "user", "content": "wait, first do this"}},
        {"t": 10, "kind": "message", "data": {"role": "assistant", "content": "ok"}},
        # distant REAL response (interleaved user): must be MOVED up, not stubbed
        # — a copy would leave the distant tool message orphaned and 400
        # ("tool must be a response to a preceding tool_calls")
        {"t": 11, "kind": "message", "data": {"role": "assistant", "content": None,
                                             "tool_calls": [{"id": "call_d", "type": "function",
                                                             "function": {"name": "bash", "arguments": "{}"}}]}},
        {"t": 12, "kind": "message", "data": {"role": "user", "content": "hold on"}},
        {"t": 13, "kind": "message", "data": {"role": "tool", "tool_call_id": "call_d",
                                             "content": "real output of call_d"}},
        {"t": 14, "kind": "message", "data": {"role": "assistant", "content": "done"}},
    ])

    hist = load_history(traj)
    roles = [m["role"] for m in hist]
    print("roles:", roles)

    # Core guard: every tool_calls answered → valid for the next request.
    assert_history_api_valid(hist)

    # In-position: call_c's stub sits BEFORE the interleaved user message.
    orphan_i = next(i for i, m in enumerate(hist)
                    if m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_c")
    assert hist[orphan_i + 1]["role"] == "tool", "stub not in-position for call_c"
    assert hist[orphan_i + 1]["tool_call_id"] == "call_c", hist[orphan_i + 1]
    assert "interrupted" in hist[orphan_i + 1]["content"]

    # Trailing orphan got stubs immediately after it, in call order.
    tail_i = next(i for i, m in enumerate(hist)
                  if m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_a")
    got = [m["tool_call_id"] for m in hist[tail_i + 1:tail_i + 3]]
    assert got == ["call_a", "call_b"], got

    # Idempotent: a second pass changes nothing (no duplicate stubs).
    before = json.dumps(hist, ensure_ascii=False)
    repair_history(hist)
    after = json.dumps(hist, ensure_ascii=False)
    assert before == after, "repair_history not idempotent"

    # The already-answered round kept its REAL output, not a stub.
    ok = [m for m in hist if m.get("tool_call_id") == "call_ok"]
    assert ok and ok[0]["content"] == "done", ok

    # call_d's distant REAL response was MOVED into position (no stub, no dup,
    # no stray tool message) — and it sits before the interleaved user msg.
    d_i = next(i for i, m in enumerate(hist)
               if m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_d")
    assert hist[d_i + 1]["role"] == "tool", "call_d response not moved in-position"
    assert hist[d_i + 1]["tool_call_id"] == "call_d", hist[d_i + 1]
    assert hist[d_i + 1]["content"] == "real output of call_d", \
        "distant real output lost — should be MOVED, not stubbed"
    assert "interrupted" not in hist[d_i + 1]["content"]

    # No duplicate call_ids anywhere (stub + distant original would 400 too).
    seen = [m["tool_call_id"] for m in hist if m.get("role") == "tool"]
    assert len(seen) == len(set(seen)), "duplicate tool_call_id after repair"

    # No stray tool message: every tool msg's preceding non-tool msg is an
    # assistant with a matching tool_calls entry.
    prev_non_tool = None
    for m in hist:
        if m.get("role") == "tool":
            assert prev_non_tool and prev_non_tool.get("role") == "assistant", \
                f"stray tool message {m.get('tool_call_id')}"
            assert prev_non_tool.get("tool_calls"), "tool msg after plain assistant"
            assert any(t["id"] == m["tool_call_id"] for t in prev_non_tool["tool_calls"]), \
                f"tool msg {m.get('tool_call_id')} not declared by preceding assistant"
        elif m.get("role") in ("assistant", "user"):
            prev_non_tool = m

    print("HARDKILL RESUME SMOKE OK — load_history self-heals orphaned tool_calls. amaze!")


main()
