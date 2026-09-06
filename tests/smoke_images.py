"""Image input smoke test: reference parts in history, base64 only on the wire.

Covers engine/images.py (content shapes, api_view inflate/flatten, dragged-path
extraction), the provider vision flags, compaction's image-aware estimates, and
a fake-client engine turn proving: history + trajectory carry image_path
references while the API request carries a base64 data URL — and a no-vision
model gets an honest text placeholder instead. No network, no API key.
"""
import asyncio
import base64
import json
import os
import tempfile
import types
from pathlib import Path

os.environ.setdefault("ROCKYCODE_HOME", tempfile.mkdtemp(prefix="rockytest-home-"))
os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.engine import images as I  # noqa: E402
from rockycode.engine import providers as P  # noqa: E402
from rockycode.engine.compaction import (  # noqa: E402
    IMAGE_PART_EST_TOKENS, estimate_msg_tokens, truncate_oversized,
)
from rockycode.engine.loop import Engine  # noqa: E402

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not-really-pixels-but-fine"
img = Path.cwd() / "shot.png"
img.write_bytes(PNG_BYTES)

# ── content shapes ───────────────────────────────────────────────────────────
assert I.make_content("hi", []) == "hi", "no images → plain string, wire-identical"
c = I.make_content("what is this?", [I.image_part(img)])
assert isinstance(c, list) and c[0] == {"type": "text", "text": "what is this?"}
assert c[1]["type"] == "image_path" and c[1]["media_type"] == "image/png"
assert I.content_text(c) == "what is this?" and I.content_text("plain") == "plain"
assert I.count_images(c) == 1 and I.count_images("plain") == 0
only_img = I.make_content("", [I.image_part(img)])
assert only_img == [I.image_part(img)], "empty text → no empty text part"
print("shapes: str passthrough / text+image_path list / content_text  ✓")

# ── api_view: vision inflates to a base64 data URL, never mutates history ────
hist = [{"role": "system", "content": "S"}, {"role": "user", "content": c}]
sent = I.api_view(hist, vision=True)
url = sent[1]["content"][1]["image_url"]["url"]
assert url.startswith("data:image/png;base64,")
assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES, "pixels survive the round trip"
assert hist[1]["content"][1]["type"] == "image_path", "history keeps the reference form"
assert sent[1]["content"][0] == {"type": "text", "text": "what is this?"}
plain = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
assert I.api_view(plain, vision=True) is plain, "no list content → zero-copy fast path"
print("api_view(vision): base64 data URL on the wire, reference in history  ✓")

# ── api_view: no vision collapses to a string with an honest placeholder ─────
flat = I.api_view(hist, vision=False)
fc = flat[1]["content"]
assert isinstance(fc, str) and "what is this?" in fc
assert "shot.png" in fc and "cannot see images" in fc, fc
gone = [{"role": "user", "content": I.make_content("x", [I.image_part("/nope/missing.png")])}]
inflated = I.api_view(gone, vision=True)[0]["content"]
assert all(p["type"] == "text" for p in inflated), "missing file degrades to text, no 400"
assert "missing.png" in inflated[1]["text"]
print("api_view(no vision / missing file): placeholder text, never a broken part  ✓")

# ── dragged/typed path extraction ────────────────────────────────────────────
spaced = Path.cwd() / "screen shot.png"
spaced.write_bytes(PNG_BYTES)
found = I.extract_image_paths(
    f'look at "{spaced}" and {img} and file://{img} plus /nope/gone.png and {img.with_suffix(".txt")}'
)
assert found == [spaced, img], found  # quoted+spaced ✓, deduped file:// ✓, missing/.txt ✗
print("extract_image_paths: quoted, bare, file:// URL; missing + non-image skipped  ✓")

# ── provider registry: vision is DATA like tools/reasoning ───────────────────
provs = P.discover()
assert provs["minimax"].vision and provs["kimi"].vision and provs["stepfun"].vision
assert not provs["deepseek"].vision and not provs["glm"].vision
assert provs["stepfun"].endpoints[0].key_env == "ROCKYCODE_STEPFUN_API_KEY"
toml = Path(tempfile.mkdtemp()) / "providers.toml"
toml.write_text('[providers.myvlm]\nmodel = "vlm-1"\nbase_url = "https://x/v1"\nvision = true\n'
                '[providers.mytext]\nmodel = "t-1"\nbase_url = "https://y/v1"\n')
custom = P._parse_toml(toml)
assert custom["myvlm"].vision and not custom["mytext"].vision, "toml vision flag parses"
print("providers: builtin vision flags (minimax/kimi/stepfun) + toml `vision = true`  ✓")

# ── compaction: image parts are estimated as pixels, text parts still bounded ─
est = estimate_msg_tokens({"role": "user", "content": c})
assert est >= IMAGE_PART_EST_TOKENS, "an image costs pixel-tokens, not len(path)"
big = [{"role": "system", "content": "S"},
       {"role": "user", "content": [{"type": "text", "text": "x" * 50_000},
                                    I.image_part(img)]}]
n = truncate_oversized(big)
assert n == 1 and len(big[1]["content"][0]["text"]) < 50_000, "giant text part bounded"
assert big[1]["content"][1]["type"] == "image_path", "image part untouched"
print("compaction: image-aware token estimate + oversized text-part truncation  ✓")

# ── engine turn with a fake client: the full wire contract ───────────────────
class FakeCompletions:
    def __init__(self):
        self.seen = []

    async def create(self, **kw):
        self.seen.append(kw["messages"])
        async def stream():
            delta = types.SimpleNamespace(reasoning_content=None, content="i see it!",
                                          tool_calls=None)
            yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)])
        return stream()


fake = FakeCompletions()
client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
eng = Engine(model="fake", client=client, workdir=Path.cwd())
assert eng.vision_enabled is False, "home model (deepseek) has no vision — default off"
eng.switch_provider(client, "kimi-k3", provider_name="kimi-en",
                    reasoning_policy="none", vision=True)
assert eng.vision_enabled is True


async def turn():
    async for _ in eng.run_turn("what is in this screenshot?", images=[img]):
        pass

asyncio.run(turn())
wire = fake.seen[0]
user_wire = next(m for m in wire if m["role"] == "user")
assert user_wire["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
assert not any("image_path" in json.dumps(m) for m in wire), "internal part never on the wire"
user_hist = next(m for m in eng.history if m["role"] == "user")
assert user_hist["content"][1]["type"] == "image_path", "history keeps the reference"
traj = eng.trajectory.path.read_text()
assert "image_path" in traj and str(img) in traj, "trajectory records the reference"
assert "base64" not in traj, "trajectory never bloats with the blob"

# same engine flipped back to a no-vision provider: honest placeholder on the wire
eng.switch_provider(client, "deepseek-v4-pro", provider_name="deepseek",
                    reasoning_policy="deepseek", vision=False)
asyncio.run((lambda: turn())())  # re-ask; history still holds the image turn
flat_user = next(m for m in fake.seen[1] if m["role"] == "user")
assert isinstance(flat_user["content"], str) and "cannot see images" in flat_user["content"]
print("engine: vision wire = base64 · trajectory = reference · no-vision = placeholder  ✓")

# ── vision router: cli backend runs shell-free, auto prefers the user's CLI ──
from rockycode.engine import vision  # noqa: E402

desc, src = asyncio.run(vision.describe_cli(img, template="echo mock-desc for {path}"))
assert "mock-desc" in desc and str(img) in desc and src == "echo"
desc2, _ = asyncio.run(vision.describe_cli(img, template="echo saw"))
assert str(img) in desc2, "template without {path} → path appended"
for bad in ("false", "definitely-not-a-real-cmd-xyz"):
    try:
        asyncio.run(vision.describe_cli(img, template=bad))
        raise AssertionError(f"{bad}: must raise VisionError")
    except vision.VisionError:
        pass
_d, src3 = asyncio.run(vision.describe(img, route="auto", template="echo via-cli {path}"))
assert src3 == "echo", "auto route prefers the configured CLI"

for v in list(os.environ):
    if v.startswith("ROCKYCODE_") and v.endswith("_API_KEY"):
        os.environ.pop(v, None)
assert vision.sidecar_choice() is None, "no keys → no provider route"
try:
    asyncio.run(vision.describe_provider(img))
    raise AssertionError("no keyed endpoint must raise VisionError")
except vision.VisionError:
    pass
os.environ["ROCKYCODE_STEPFUN_API_KEY"] = "sk-rocky-step"
os.environ["ROCKYCODE_KIMI_CN_API_KEY"] = "sk-rocky-kimi"
assert vision.sidecar_choice() is not None and vision.sidecar_choice().provider.vision
assert vision.sidecar_choice("kimi-cn").prov_id == "kimi-cn", "config image_provider honored"
picked = vision.sidecar_choice("kimi-cn:k3")  # endpoint:model spec, substring ok
assert picked.prov_id == "kimi-cn" and picked.model == "kimi-k3", "image model pinnable"
assert vision.sidecar_choice("kimi-cn:nope") is not None, "unmatched model → keyed fallback"
os.environ.pop("ROCKYCODE_STEPFUN_API_KEY")
os.environ.pop("ROCKYCODE_KIMI_CN_API_KEY")
print("vision: shell-free cli template · auto-prefers-cli · keyed sidecar pick  ✓")

# ── described part: flatten emits the description, vision still gets pixels ──
part = I.image_part(img)
part["description"], part["described_by"] = "a purple button, misaligned 3px", "mmx"
h2 = [{"role": "user", "content": [{"type": "text", "text": "review"}, part]}]
flat2 = I.api_view(h2, vision=False)[0]["content"]
assert "as seen by mmx" in flat2 and "misaligned 3px" in flat2
assert "cannot see" not in flat2, "described image never shows the NOT-sent placeholder"
assert I.api_view(h2, vision=True)[0]["content"][1]["type"] == "image_url"
print("described part: no-vision wire carries the description, vision the pixels  ✓")

# ── view_image tool: registered, approval-gated, jailed, routed via image_cli ─
from rockycode import config as C  # noqa: E402
from rockycode.engine import tools as T  # noqa: E402

C.GLOBAL_PATH = Path(tempfile.mkdtemp()) / "config.toml"  # never the real ~/.rockycode
assert C.DEFAULTS["image_route"] == "ask"
_v, cerr = C.set_value("image_route", "nope")
assert cerr and "allowed" in cerr
_v, cerr = C.set_value("image_cli", "echo tool-eyes {path}")
assert cerr is None
reg = T.build_registry(Path.cwd())
assert "view_image" in reg and reg["view_image"].risk == "risky", \
    "pixels leave the machine → approval-gated, not an auto-allowed read"
out, ok = asyncio.run(T.execute(reg, "view_image", json.dumps({"path": str(img)})))
assert ok and "tool-eyes" in out and "seen by echo" in out, out
out, _ok = asyncio.run(T.execute(reg, "view_image", json.dumps({"path": "/etc/passwd"})))
assert "blocked" in out, "read jail applies"
out, ok = asyncio.run(T.execute(reg, "view_image", json.dumps({"path": "nope.png"})))
assert not ok and "not found" in out
print("view_image: registered + risky + jailed + routed through image_cli  ✓")

# ── config hygiene + known CLIs: "/config image_cli mmx" is the whole setup ──
import tomllib  # noqa: E402

from rockycode.engine.vision import DESCRIBE_PROMPT, _cli_argv  # noqa: E402

# Shell-habit quotes are stripped on set, and the stored file stays valid TOML
# (an unescaped quote once corrupted the WHOLE config into silent defaults).
_v, cerr = C.set_value("image_cli", '"mmx vision describe --image {path}"')
assert cerr is None and _v == "mmx vision describe --image {path}", _v
assert tomllib.loads(C.GLOBAL_PATH.read_text())["image_cli"] == _v
_v, cerr = C.set_value("image_cli", 'has "inner" quotes')
assert cerr is None
assert tomllib.loads(C.GLOBAL_PATH.read_text())["image_cli"] == 'has "inner" quotes'

# A bare known tool name expands to its real, --help-grounded invocation.
argv = _cli_argv("mmx", img, "what breed?")
assert argv == ["mmx", "vision", "describe", "--image", str(img),
                "--prompt", "what breed?"], argv
argv = _cli_argv("mmx", img, "")  # no question → the dense default prompt,
assert argv[-1] == DESCRIBE_PROMPT and "--prompt" in argv, argv  # never an empty --prompt
argv = _cli_argv("sometool", img, "")  # unknown bare tool → path appended last
assert argv == ["sometool", str(img)], argv
print("config: quotes stripped + TOML-safe · image_cli mmx expands via KNOWN_CLIS  ✓")

# ── route picker: choices reflect which backends exist ───────────────────────
from rockycode.tui.app import ImageRouteChoice  # noqa: E402


async def _mk(provider_label, cli_label):
    fut = asyncio.get_running_loop().create_future()
    w = ImageRouteChoice(2, provider_label, cli_label, fut)
    return [v for v, _t in w._choices]

assert asyncio.run(_mk("minimax-cn:minimax-m3", "mmx describe {path}")) == \
    ["provider", "provider_always", "cli", "cli_always", "skip"]
assert asyncio.run(_mk("minimax-cn:minimax-m3", "")) == ["provider", "provider_always", "skip"]
assert asyncio.run(_mk("", "mmx")) == ["cli", "cli_always", "skip"]
print("route picker: rows track available backends, skip always present  ✓")

# ── view_image on a VISION model: attach the pixels, no describe middleman ───
# The model decides what matters in the image itself — the tool returns the
# attach marker, the engine rewrites the tool response and appends the real
# image as the NEXT user message (images ride user messages only).
flag = {"on": True}
reg_v = T.build_registry(Path.cwd(), vision_active=lambda: flag["on"])
out, ok = asyncio.run(T.execute(reg_v, "view_image", json.dumps({"path": str(img)})))
assert ok and out.startswith(T.VIEW_ATTACH_PREFIX) and out.endswith(str(img)), out
flag["on"] = False  # live flip — same registry, /model switch, no rebuild
C.set_value("image_cli", "echo tool-eyes {path}")  # earlier sections changed it
out, ok = asyncio.run(T.execute(reg_v, "view_image", json.dumps({"path": str(img)})))
assert "tool-eyes" in out, "text model → back to the sidecar describe route"

eng_v = Engine(model="fake", client=client, workdir=Path.cwd())
pending: list = []
tool_text = eng_v._attach_rewrite("view_image", f"{T.VIEW_ATTACH_PREFIX}{img}", pending)
assert pending == [str(img)] and "attached" in tool_text
assert T.VIEW_ATTACH_PREFIX not in tool_text, "marker must never reach history"
assert eng_v._attach_rewrite("bash", "[image-attach] hi", []) == "[image-attach] hi", \
    "only view_image's own output is rewritten"
hist_len = len(eng_v.history)
eng_v._attach_images(pending)
attached = eng_v.history[-1]
assert len(eng_v.history) == hist_len + 1 and attached["role"] == "user"
assert I.count_images(attached["content"]) == 1, "the real image rides a user message"
assert I.api_view(eng_v.history, vision=True)[-1]["content"][-1]["type"] == "image_url"
eng_v._attach_images([])
assert len(eng_v.history) == hist_len + 1, "no attachments → no empty user message"
print("view_image(vision): raw pixels attached as the next user message  ✓")

# ── downscale_for_api: tiny files untouched; failures degrade to the original ─
assert I.downscale_for_api(img) == img, "small file → original path, no copy"
assert I.downscale_for_api("/nope/gone.png") == Path("/nope/gone.png"), \
    "missing file → original path, never a raise"
print("downscale_for_api: best-effort — never blocks an attach  ✓")

print("IMAGES SMOKE OK — native sees pixels; the rest ask another key, a CLI, or say so. amaze!")
