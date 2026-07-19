"""Redaction smoke test (engine/redact.py). No network, no real secrets.

Covers the existing shapes plus the new ones (#8: Google AIza keys, JWTs, and a
generic high-entropy token heuristic) — and, just as important, the strings that
must NOT be redacted so the coding agent stays useful (git SHAs, hex digests,
plain prose, short ids).
"""
from rockycode.engine.redact import redact


def redacted(text: str) -> bool:
    return "[redacted" in redact(text)


# --- existing shapes still fire ---
assert "[redacted: api key]" in redact("key is sk-abcdef0123456789ABCDEF")
assert "[redacted: github token]" in redact("token ghp_" + "A" * 30)
assert "[redacted: aws key id]" in redact("AKIA" + "ABCD1234EFGH5678")
assert "Bearer [redacted]" in redact("Authorization: Bearer abcdef0123456789ABCDEF")
assert "[redacted]" in redact("ACCESS_TOKEN=hunter2secretvalue")
print("redact: existing shapes (sk-, ghp_, AKIA, Bearer, NAME=value)  ✓")

# --- #8 Google AIza key ---
aiza = "AIza" + "Sy" + "A" * 33  # AIza + 35 chars
out = redact(f"GOOGLE_MAPS=\n{aiza}\ndone")
assert "[redacted: google api key]" in out and aiza not in out, out
print("redact: Google AIza… key  ✓")

# --- #8 JWT ---
jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
       ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
       ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
out = redact(f"cookie set: {jwt}")
assert "[redacted: jwt]" in out and jwt not in out, out
print("redact: JWT  ✓")

# --- #8 generic high-entropy token (mixed lower+UPPER+digit, 32–64 chars) ---
tok = "Ab3" + "xY7kLm9QpZ2rTn5wVc8bDf4gHj6sKl0" + "Zq1"  # 37 chars, all 3 classes
assert len(tok) >= 32 and redacted(tok), (len(tok), redact(tok))
print("redact: generic high-entropy token  ✓")

# --- MUST NOT redact: these are not secrets and the agent needs them ---
git_sha = "e83c5163316f89bfbde7d9ab23ca2e25604af290"      # 40 hex, no uppercase
md5 = "d41d8cd98f00b204e9800998ecf8427e"                    # 32 hex
allcaps = "THISISALONGCONSTANTNAMEWITHOUTANYDIGITSXY"       # no lower/digit
decimal = "12345678901234567890123456789012345678"         # digits only
prose = "the quick brown fox jumps over the lazy dog again and again today"
uuid = "550e8400-e29b-41d4-a716-446655440000"              # dashes split runs
for s in (git_sha, md5, allcaps, decimal, prose, uuid):
    assert not redacted(s), f"over-redacted a non-secret: {s!r} -> {redact(s)!r}"
print("redact: git SHA / md5 / ALLCAPS / decimal / prose / uuid preserved  ✓")

# --- known env value pass still wins (exact value, any name/shape) ---
import os

os.environ["ROCKYCODE_TEST_TOKEN"] = "supersecretenvvalue123"  # lowercase → not a shape
try:
    out = redact("printenv shows supersecretenvvalue123 here")  # reads os.environ live
    assert "[redacted]" in out and "supersecretenvvalue123" not in out, out
finally:
    del os.environ["ROCKYCODE_TEST_TOKEN"]
print("redact: known env value scrubbed by exact-value pass  ✓")

print("REDACT SMOKE OK — amaze amaze amaze!")
