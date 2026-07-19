"""First-run onboarding: placeholder-aware key detection, credential save (0600),
and the no-op-when-configured path. The interactive prompt needs a tty, so we
test the logic around it."""
import os
import stat
import tempfile
from pathlib import Path

from rockycode import onboarding

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain

for name in ("OPENAI_API_KEY", "ROCKYCODE_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(name, None)

# key detection: nothing set → unset; a template placeholder → still unset
assert onboarding.current_key() is None and not onboarding.is_configured()
os.environ["ROCKYCODE_API_KEY"] = "sk-replace-me"
assert onboarding.current_key() is None, "placeholder must count as unset (triggers setup)"
os.environ["ROCKYCODE_API_KEY"] = "sk-realkey123"
assert onboarding.current_key() == "sk-realkey123" and onboarding.is_configured()
os.environ.pop("ROCKYCODE_API_KEY", None)
print("key detection: placeholder-aware  ✓")

# save_credentials: 0600 dotenv with the right content, applied to the process
d = Path(tempfile.mkdtemp())
p = d / ".env"
for v in ("ROCKYCODE_BASE_URL", "ROCKYCODE_MODEL"):
    os.environ.pop(v, None)
onboarding.save_credentials("sk-abc", "https://api.deepseek.com/v1", "deepseek-v4-flash", path=p)
content = p.read_text()
assert "ROCKYCODE_API_KEY=sk-abc" in content and "ROCKYCODE_MODEL=deepseek-v4-flash" in content, content
assert "ROCKYCODE_BASE_URL=https://api.deepseek.com/v1" in content and "OPENAI_BASE_URL" not in content, content
assert stat.S_IMODE(p.stat().st_mode) == 0o600, oct(p.stat().st_mode)
assert os.environ["ROCKYCODE_API_KEY"] == "sk-abc" and onboarding.is_configured()
print("save_credentials: 0600 dotenv written + applied to process  ✓")

# setup probe: an unreachable endpoint must never block setup (offline ≠ invalid)
assert onboarding._key_rejected("sk-any", "http://127.0.0.1:1/v1") is False
print("setup probe: unreachable endpoint never blocks (can't verify != invalid)  ✓")

# run_setup must NOT prompt (or hang) when a key is already configured
from rich.console import Console  # noqa: E402
onboarding.run_setup(Console(file=open(os.devnull, "w")))  # returns immediately
print("run_setup: no-op when already configured (existing users never prompted)  ✓")

print("ONBOARDING SMOKE OK — first-run key setup. amaze!")
