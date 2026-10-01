"""The effort dial: off/low/high/max is rocky-owned and provider-neutral.
The dial value is clamped onto each provider's own tiers by POSITION
(OpenAI-style low|medium|high gets medium for rocky's high), `off` must mean
thinking disabled with no effort field on the wire (or the lowest tier where
thinking can't be off), xhigh stays accepted as max, and the engine must read
the dial fresh on every call so a live /effort flip lands on the next request."""
import os
import tempfile

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine.effort import (
    CLI_EFFORTS, EFFORT_LEVELS, build_extra_body, clamp, normalize, to_deepseek, wire_tier,
)

# the dial and the CLI flag values stay in sync (`off` is spelled --no-thinking)
assert EFFORT_LEVELS == ("off",) + CLI_EFFORTS == ("off", "low", "high", "max")
assert normalize("xhigh") == "max", "the pre-V4.1 dial word still works"

# clamp by position: rocky's tiers pass; OpenAI-style gets medium for high
assert clamp("high", ("low", "high", "max")) == "high"
assert clamp("high", ("low", "medium", "high")) == "medium"
assert clamp("max", ("low", "medium", "high")) == "high"
assert clamp("low", ("low", "medium", "high")) == "low"
assert clamp("high", ("high", "max")) == "high" and clamp("max", ("high", "max")) == "max"
assert clamp("medium", ("low", "high", "max")) == "medium", "unknown words pass through"
assert to_deepseek("xhigh") == "max" and to_deepseek("low") == "low"
assert wire_tier("high", "openai") == "medium" and wire_tier("max", "minimax") is None
print("clamp: positional onto provider tiers; legacy xhigh = max  ✓")

# wire shape: thinking on carries the CLAMPED effort; off carries none at all
assert build_extra_body(True, "xhigh") == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
assert build_extra_body(True, "low") == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
assert build_extra_body(False, "max") == {"thinking": {"type": "disabled"}}
assert build_extra_body(False, "max", "thinking", thinking_off=False) == {
    "thinking": {"type": "enabled"}, "reasoning_effort": "low"}
assert build_extra_body(False, "max", "effort") == {"reasoning_effort": "low"}
assert build_extra_body(False, "max", "enable_thinking") == {"enable_thinking": False}
assert build_extra_body(False, "max", "minimax") == {"thinking": {"type": "disabled"}}
print("build_extra_body: enabled sends the clamped tier; off per shape  ✓")

# live flip: /effort mutates engine attrs between turns; _extra_body must
# reflect the change immediately (it re-reads the dial per call)
from rockycode.engine.loop import Engine  # noqa: E402

eng = Engine("deepseek-v4-pro", client=object(), registry={})
assert eng._extra_body() == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
eng.reasoning_effort = "xhigh"
assert eng._extra_body()["reasoning_effort"] == "max", "xhigh must land as max at the wire"
eng.reasoning_effort = "low"
assert eng._extra_body()["reasoning_effort"] == "low"
eng.thinking = False
assert eng._extra_body() == {"thinking": {"type": "disabled"}}
eng.thinking = True  # /effort off keeps the last tier for when it's back on
assert eng._extra_body()["reasoning_effort"] == "low"
# the engine constructed with the legacy word normalizes it once
assert Engine("x", client=object(), registry={}, reasoning_effort="xhigh").reasoning_effort == "max"
print("engine: live dial changes land on the very next request  ✓")

print("amaze! effort dial ok")
