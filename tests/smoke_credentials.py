"""Credential namespace: rocky reads exactly ONE key name (ROCKYCODE_API_KEY)
and ONE endpoint name (ROCKYCODE_BASE_URL), from ~/.rockycode or the shell —
never from ambient OPENAI_* vars and never from a project folder. An ambient
OPENAI_API_KEY is usually a REAL OpenAI credential (using it ships it to a
DeepSeek endpoint); a project-controlled base URL is a key-exfiltration vector
(whoever controls the URL receives the Authorization header). No legacy
migration: the var names were finalized before going public."""
import os
import tempfile
from pathlib import Path

from rockycode import onboarding

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain


def _clear() -> None:
    for n in ("ROCKYCODE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(n, None)


# the misrouting fix: foreign provider keys in the ambient env are never used
_clear()
os.environ["OPENAI_API_KEY"] = "sk-real-openai-key"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-real-anthropic-token"
assert onboarding.current_key() is None and not onboarding.is_configured(), \
    "ambient foreign keys must be invisible to rocky"
os.environ["ROCKYCODE_API_KEY"] = "sk-rocky"
assert onboarding.current_key() == "sk-rocky"
assert onboarding.current_key_source() == "ROCKYCODE_API_KEY"
print("only ROCKYCODE_API_KEY is read — ambient OPENAI/ANTHROPIC keys ignored  ✓")

# require_key: unconfigured → RuntimeError that spells out the fix
_clear()
try:
    onboarding.require_key()
    raise AssertionError("require_key must raise when unconfigured")
except RuntimeError as e:
    assert "ROCKYCODE_API_KEY" in str(e), str(e)
print("require_key: actionable error, no SDK env-trawling fallback  ✓")

# endpoint: only ROCKYCODE_BASE_URL (or the DeepSeek default) — the ambient
# OPENAI_BASE_URL must be invisible, or a foreign env silently redirects the key
for v in ("ROCKYCODE_BASE_URL", "OPENAI_BASE_URL"):
    os.environ.pop(v, None)
os.environ["OPENAI_BASE_URL"] = "https://evil.example/v1"
assert onboarding.require_base_url() == "https://api.deepseek.com/v1", \
    "ambient OPENAI_BASE_URL must never route rocky's key"
os.environ["ROCKYCODE_BASE_URL"] = "https://proxy.mine/v1"
assert onboarding.require_base_url() == "https://proxy.mine/v1"
for v in ("ROCKYCODE_BASE_URL", "OPENAI_BASE_URL"):
    os.environ.pop(v, None)
print("endpoint: ROCKYCODE_BASE_URL or default — ambient OPENAI_BASE_URL invisible  ✓")

# project .env: never loaded; credential-shaped NAMES get a warning, values
# never appear in the warning text
d = Path(tempfile.mkdtemp())
(d / ".env").write_text(
    "OPENAI_API_KEY=sk-secret-value\n"
    "OPENAI_BASE_URL=https://evil.example/v1\n"
    "export ANTHROPIC_AUTH_TOKEN=tok-hush\n"
    "FLASK_DEBUG=1\n"
)
warns = onboarding.project_env_warnings(d)
assert len(warns) == 3, warns
assert not any(("sk-secret-value" in w or "evil.example" in w or "tok-hush" in w) for w in warns), \
    "warning must name the var, never echo its value: " + str(warns)
assert onboarding.project_env_warnings(Path(tempfile.mkdtemp())) == [], "no .env → no warnings"
print("project .env: ignored, warned by NAME only  ✓")

# cli must not slurp the cwd .env at import time — that was the shadowing bug
_ROOT = Path(__file__).resolve().parents[1] / "rockycode"
cli_src = (_ROOT / "cli.py").read_text()
assert "load_dotenv()" not in cli_src, "cli must never load a project .env"
print("cli: no bare load_dotenv() — project env files are never read  ✓")

# static audit: EVERY OpenAI client in the codebase pins api_key= AND base_url=
# explicitly, so the SDK's env fallbacks can never decide what goes where
import re

bad = []
for f in sorted(_ROOT.rglob("*.py")):
    src = f.read_text()
    for m in re.finditer(r"\b(?:Async)?OpenAI\(", src):
        window = src[m.start(): m.start() + 250]
        if "api_key=" not in window or "base_url=" not in window:
            bad.append(f"{f.relative_to(_ROOT)}: {window.splitlines()[0]}")
assert not bad, "clients missing explicit api_key=/base_url=: " + str(bad)
print("audit: every OpenAI client pins api_key= and base_url= explicitly  ✓")

# source attribution: bootstrap must record where the key CAME from — after
# the keychain is copied into the process env, post-hoc checks can't tell
# (that ambiguity sent a real debugging session down the wrong branch)
onboarding.GLOBAL_ENV = Path(tempfile.mkdtemp()) / ".env"  # never the real one

_clear(); onboarding._KEY_SOURCE = None
onboarding._keyring_key = lambda: "sk-from-chain"
onboarding.bootstrap_credentials()
assert onboarding.current_key_source() == "keychain", onboarding.current_key_source()

_clear(); onboarding._KEY_SOURCE = None
onboarding._keyring_key = lambda: None
onboarding.GLOBAL_ENV.write_text("ROCKYCODE_API_KEY=sk-from-file\n")
onboarding.bootstrap_credentials()
assert str(onboarding.current_key_source()).endswith(".env"), onboarding.current_key_source()

_clear(); onboarding._KEY_SOURCE = None
os.environ["ROCKYCODE_API_KEY"] = "sk-from-shell"
onboarding.bootstrap_credentials()
assert onboarding.current_key_source() == "shell export", onboarding.current_key_source()
print("attribution: keychain / global .env / shell export named truthfully  ✓")

# auth-class API errors name the key SOURCE and endpoint — never the key
import asyncio
import types

_clear(); onboarding._KEY_SOURCE = None  # fallback path: coarse env detection
os.environ["ROCKYCODE_API_KEY"] = "sk-rocky-test"


class _Auth400(Exception):
    status_code = 400


async def _boom(**kw):
    raise _Auth400("Error code: 400")


from rockycode.engine.loop import Engine  # noqa: E402

_client = types.SimpleNamespace(chat=types.SimpleNamespace(
    completions=types.SimpleNamespace(create=_boom)))
_eng = Engine(model="fake", client=_client, workdir=Path(tempfile.mkdtemp()))


async def _one_turn():
    async for ev in _eng.run_turn("hi"):
        if type(ev).__name__ == "EngineError":
            return ev.message
    return ""

_msg = asyncio.run(_one_turn())
assert "key source: ROCKYCODE_API_KEY" in _msg, _msg
assert "api.deepseek.com" in _msg, _msg
assert "sk-rocky-test" not in _msg, "the key itself must never surface"
_clear()
print("errors: 400/401 name the key source + endpoint, never the key  ✓")

print("CREDENTIALS SMOKE OK — one key name, one endpoint name, no ambient pickup. amaze!")
