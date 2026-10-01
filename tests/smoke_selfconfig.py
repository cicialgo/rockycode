"""rocky configures rocky: the `rocky_config` tool + the rocky-setup skill.

Every write lands under a throwaway ROCKYCODE_HOME through the validated
setters; key-shaped values are refused with the env var to use instead. No
network, no Docker.
"""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockytest-home-")

from rockycode import config as C
from rockycode.engine import providers as P
from rockycode.engine.selfconfig import build_selfconfig_tool
from rockycode.engine.skills import discover_skills
from rockycode.prompts.rocky import TOOL_HINTS

home = Path(os.environ["ROCKYCODE_HOME"])
C.GLOBAL_PATH = home / "config.toml"
P.MODELS_TOML = home / "models.toml"
P.ENDPOINTS_TOML = home / "endpoints.toml"
P.PROVIDERS_TOML = home / "providers.toml"

tool = build_selfconfig_tool(Path.cwd())
assert tool.name == "rocky_config" and tool.risk == "risky", "ask-tier: the user approves each change"
assert "rocky_config" in TOOL_HINTS
run = lambda **kw: asyncio.run(tool.fn(**kw))  # noqa: E731

# set: validated config keys, saved to the global file
out = run(action="set", key="model", value="glm:flash")
assert out.startswith("[ok]") and C.load()["model"] == "glm:flash", out
out = run(action="set", key="image_route", value="banana")
assert out.startswith("[error]") and "allowed" in out
out = run(action="set", key="max_tokens", value="0")
assert out.startswith("[ok]") and "follow the model" in out
print("rocky_config set: validated keys, global config only  ✓")

# set_url: an own base URL for a known provider → <provider>-custom
out = run(action="set_url", provider="deepseek", base_url="https://proxy.example/v1")
assert out.startswith("[ok]") and "deepseek-custom" in out, out
assert P.resolve("deepseek-custom")[1].base_url == "https://proxy.example/v1"
out = run(action="set_url", provider="nope", base_url="https://x/v1")
assert out.startswith("[error]") and "add_provider" in out
out = run(action="set_url", provider="deepseek", base_url="")
assert out.startswith("[ok]") and "removed" in out
print("rocky_config set_url: own URL row added and removed  ✓")

# add_provider: a custom server lands in the override; the key stays a NAME
out = run(action="add_provider", provider="box", base_url="http://box:8000/v1",
          models="my-7b, my-70b", reasoning="none", context=32768)
assert out.startswith("[ok]") and "ROCKYCODE_BOX_API_KEY" in out, out
box = P.discover()["box"]
assert box.models == ["my-7b", "my-70b"] and box.spec("my-7b").context == 32768
assert "sk-" not in P.MODELS_TOML.read_text()
print("rocky_config add_provider: custom server registered, key by name only  ✓")

# keys never pass through: refused with the exact env line
for val in ("sk-abcdefghijklmnopqrstuvwxyz123456", "key_ABCDEFGHIJKLMNOP1234",
            "A" * 48):
    out = run(action="set", key="model", value=val)
    assert out.startswith("[refused]") and "ROCKYCODE_API_KEY" in out, out
out = run(action="add_provider", provider="kimi", base_url="https://api.moonshot.cn/v1",
          models="sk-mykeyabcdefghijklmnopqrstuv")
assert out.startswith("[refused]") and "ROCKYCODE_KIMI_API_KEY" in out
assert C.load()["model"] == "glm:flash", "a refused call changes nothing"
print("rocky_config: key-shaped values refused, env var named  ✓")

# show: names, never values
os.environ["ROCKYCODE_KIMI_API_KEY"] = "sk-rocky-kimi-secret-value"
out = run(action="show")
assert "kimi" in out and "key set (ROCKYCODE_KIMI_API_KEY)" in out and "secret-value" not in out
assert "models.toml" in out and "endpoints.toml" in out
os.environ.pop("ROCKYCODE_KIMI_API_KEY")
print("rocky_config show: state without secrets  ✓")

# the built-in skill ships and teaches the file map
skills = {s.name: s for s in discover_skills(Path(tempfile.mkdtemp()), home=home)}
assert "rocky-setup" in skills, list(skills)
body = Path(skills["rocky-setup"].path).read_text() if hasattr(skills["rocky-setup"], "path") else ""
print("rocky-setup skill: built-in, discoverable  ✓")

print("SELFCONFIG SMOKE OK — rocky configures rocky, never a key. amaze!")
