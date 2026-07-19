"""The effort dial: off/high/xhigh/max is rocky-owned and provider-neutral.
xhigh must reach DeepSeek as max (V4 only knows high|max), `off` must mean
thinking disabled with no effort field on the wire, and the engine must read
the dial fresh on every call so a live /effort flip lands on the next request."""
import os
import tempfile

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine.effort import CLI_EFFORTS, EFFORT_LEVELS, build_extra_body, to_deepseek

# the dial and the CLI flag values stay in sync (`off` is spelled --no-thinking)
assert EFFORT_LEVELS == ("off",) + CLI_EFFORTS

# DeepSeek clamp: xhigh → max, native tiers pass, unknown tiers pass through
assert to_deepseek("high") == "high"
assert to_deepseek("xhigh") == "max"
assert to_deepseek("max") == "max"
assert to_deepseek("medium") == "medium", "unknown tiers pass through for future providers"
print("to_deepseek: xhigh clamps to max, everything else passes  ✓")

# wire shape: thinking on carries the CLAMPED effort; off carries none at all
assert build_extra_body(True, "xhigh") == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
assert build_extra_body(True, "high") == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
assert build_extra_body(False, "max") == {"thinking": {"type": "disabled"}}
print("build_extra_body: enabled sends the clamped tier; disabled sends no effort  ✓")

# live flip: /effort mutates engine attrs between turns; _extra_body must
# reflect the change immediately (it re-reads the dial per call)
from rockycode.engine.loop import Engine  # noqa: E402

eng = Engine("deepseek-v4-pro", client=object(), registry={})
assert eng._extra_body() == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
eng.reasoning_effort = "xhigh"
assert eng._extra_body()["reasoning_effort"] == "max", "xhigh must clamp at the wire"
eng.reasoning_effort = "high"
assert eng._extra_body()["reasoning_effort"] == "high"
eng.thinking = False
assert eng._extra_body() == {"thinking": {"type": "disabled"}}
eng.thinking = True  # /effort off keeps the last tier for when it's back on
assert eng._extra_body()["reasoning_effort"] == "high"
print("engine: live dial changes land on the very next request  ✓")

print("amaze! effort dial ok")
