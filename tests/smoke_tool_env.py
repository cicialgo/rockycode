"""Tool environment: what rocky's store loaded never reaches a process rocky
starts on the model's behalf (onboarding.tool_env → shell tool, code checks,
MCP and LSP servers).

Redaction keeps a key out of tool OUTPUT; this keeps it out of the child's
environment, so a command cannot use what it cannot read. The user's own shell
exports must still pass through — a coding agent's shell runs their project.
Hermetic: a throwaway store, fake values, no keychain, no network."""
import asyncio
import os
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp())
os.environ["ROCKYCODE_HOME"] = str(home)  # before the import: the store path is fixed there
(home / ".env").write_text(
    "ROCKYCODE_API_KEY=from-store\n"
    "ROCKYCODE_KIMI_API_KEY=from-store\n"
    "GATEWAY_AUTH=from-store\n"  # a stored secret under a name no pattern would catch
    "ROCKYCODE_MODEL=deepseek-flash\n"
    "SHARED_NAME=from-store\n"   # also exported in the shell below: the shell wins
)

from rockycode import onboarding  # noqa: E402
from rockycode.engine import checks, mcp, tools  # noqa: E402

onboarding._keyring_key = lambda: None  # hermetic — never read the real OS keychain

STORED = ("ROCKYCODE_API_KEY", "ROCKYCODE_KIMI_API_KEY", "GATEWAY_AUTH", "ROCKYCODE_MODEL")
for name in (*STORED, "ROCKYCODE_BASE_URL"):
    os.environ.pop(name, None)
os.environ.update({
    "GITHUB_TOKEN": "shell-own",               # the user's own export: theirs to use
    "SHARED_NAME": "shell-own",
    "ROCKYCODE_MINIMAX_API_KEY": "shell-own",  # rocky's key, exported by hand
    "ROCKYCODE_MAX_TOKENS": "4096",            # a setting, not a credential
})
onboarding.bootstrap_credentials()
onboarding.bootstrap_credentials()  # idempotent: a second call forgets nothing
assert os.environ["ROCKYCODE_API_KEY"] == "from-store", "rocky itself keeps its key"

GONE = (*STORED, "ROCKYCODE_MINIMAX_API_KEY")
KEPT = ("GITHUB_TOKEN", "SHARED_NAME", "ROCKYCODE_MAX_TOKENS", "ROCKYCODE_HOME", "PATH")
env = onboarding.tool_env()
assert not [n for n in GONE if n in env], [n for n in GONE if n in env]
assert not [n for n in KEPT if n not in env], [n for n in KEPT if n not in env]
assert env["SHARED_NAME"] == "shell-own"
print("tool_env: the store's names and rocky's own keys are out, the user's exports stay  ✓")


def names(output: str) -> set[str]:
    return {line.split("=", 1)[0] for line in output.splitlines() if "=" in line}


# The real shell tool: `env` is the simplest command that would show a leak.
out = asyncio.run(tools._bash("env", workdir=home))
assert "from-store" not in out, "a stored value reached the shell tool"
assert not names(out) & set(GONE), names(out) & set(GONE)
assert {"GITHUB_TOKEN", "ROCKYCODE_MAX_TOKENS"} <= names(out)
print("bash: a command sees the user's environment and none of rocky's keys  ✓")

# Code checks read the project's files; they get the same scrubbed environment.
rc, out = asyncio.run(checks._run_one(["env"], home, 10))
assert rc == 0 and "from-store" not in out and "GITHUB_TOKEN" in names(out), (rc, names(out))
print("check_code: the checker's environment carries no stored key  ✓")

# MCP and LSP servers: the stored secret goes whatever it is called, the name
# pattern still drops the user's secret-looking exports, a server's own env wins.
child = mcp._safe_child_env({"SERVER_API_KEY": "explicit"})
assert "GATEWAY_AUTH" not in child and "GITHUB_TOKEN" not in child
assert child["SERVER_API_KEY"] == "explicit" and "PATH" in child
print("mcp/lsp: stored names gone by provenance, explicit server env still wins  ✓")

# First-run setup applies the new values to this process: they are the store's too.
onboarding.save_credentials("setup-key", "https://example.invalid/v1", "deepseek-flash",
                            path=home / "fresh.env")
assert os.environ["ROCKYCODE_BASE_URL"] == "https://example.invalid/v1"
assert "ROCKYCODE_BASE_URL" not in onboarding.tool_env()
print("setup: values applied by first-run setup stay in rocky's process  ✓")

print("TOOL ENV SMOKE OK — rocky keep key, command no see key. amaze!")
