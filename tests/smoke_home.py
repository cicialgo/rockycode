"""ROCKYCODE_HOME moves everything rocky owns, not most of it.

The settings file, the price overrides and the goal logs used to stay under
~/.rockycode whatever the variable said. For a user who relocates the store
that is a split store; for a test it is a write into the real one.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
home = Path(tempfile.mkdtemp(prefix="rocky-home-"))
probe = (
    "from rockycode import config, onboarding, pricing, session\n"
    "from rockycode.engine import providers\n"
    "print(config.GLOBAL_PATH); print(pricing.OVERRIDE_PATH); print(session.HOME_ROOT / 'goal-logs')\n"
    "print(onboarding.GLOBAL_ENV); print(providers.MODELS_TOML); print(session.REGISTRY)\n"
    "config.set_value('language', 'en')\n"
)
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT,
                     env={**os.environ, "ROCKYCODE_HOME": str(home), "PYTHONPATH": str(ROOT)})
assert out.returncode == 0, out.stderr
paths = [Path(line) for line in out.stdout.split()]
assert len(paths) == 6 and all(home in p.parents for p in paths), paths
assert (home / "config.toml").read_text() == 'language = "en"\n', "a saved setting lands in the relocated home"
print("home: settings, price overrides, goal logs, keys, model catalog and projects all follow ROCKYCODE_HOME  ✓")

# No module may reach for ~/.rockycode without asking the variable first.
loose = []
for f in sorted((ROOT / "rockycode").rglob("*.py")):
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r'Path\.home\(\)\s*/\s*"\.rockycode"', line) and not re.search(
                r"ROCKYCODE_HOME|_HOME_ENV|if base else", line):
            loose.append(f"{f.relative_to(ROOT)}:{n}")
assert not loose, f"paths that ignore ROCKYCODE_HOME: {loose}"
print("audit: nothing in the package hard-codes ~/.rockycode  ✓")

print("HOME SMOKE OK — one home, and it moves whole. amaze!")
