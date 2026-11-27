#!/bin/sh
# Secret sweep over a tree (default: the repo). Uses gitleaks when it is on
# PATH (config: .gitleaks.toml), otherwise a conservative grep for well-known
# credential shapes. Prints file:line ONLY — never the matched text — and
# exits 1 on any hit. Used by scripts/public-export.sh; run it by hand before
# any push you are unsure about:
#
#   ./scripts/secret-sweep.sh            # this checkout
#   ./scripts/secret-sweep.sh ../tree    # an exported tree
#
set -eu
DIR="${1:-.}"
DIR=$(cd "$DIR" && pwd)

if command -v gitleaks >/dev/null 2>&1; then
  CFG=""
  [ -f "$DIR/.gitleaks.toml" ] && CFG="--config $DIR/.gitleaks.toml"
  # --redact keeps secrets out of the terminal/log; --no-git scans files as-is.
  if gitleaks detect --no-git --source "$DIR" --redact --exit-code 1 $CFG >/dev/null 2>&1; then
    echo "  secret sweep (gitleaks): clean"
    exit 0
  fi
  echo "✗ secret sweep (gitleaks) found something — run: gitleaks detect --no-git --source $DIR --redact $CFG" >&2
  exit 1
fi

# Fallback. Synthetic secrets in the redaction tests are allowlisted — keep
# this list in sync with .gitleaks.toml.
ALLOW='tests/smoke_jev_contract\.py|tests/smoke_redact\.py|tests/smoke_selfconfig\.py|tests/smoke_providers\.py|tests/smoke_credentials\.py|tests/smoke_mcp_server\.py|tests/smoke_mcp_capabilities\.py|apps/desktop/runtime/tests/test_shared_runtime\.py'
Q="[\"']"
PAT="sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[0-9A-Za-z-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|(api[_-]?key|secret|token|password)[[:space:]]*[:=][[:space:]]*${Q}[A-Za-z0-9_-]{16,}${Q}"
hits=$(cd "$DIR" && grep -rnIE "$PAT" . --exclude-dir=.git --exclude-dir=node_modules --exclude='*.lock' --exclude='package-lock.json' 2>/dev/null | cut -d: -f1,2 | grep -vE "^\./($ALLOW):" || true)
if [ -n "$hits" ]; then
  echo "✗ secret sweep (grep fallback) — credential-shaped strings at:" >&2
  echo "$hits" | sed 's/^/    /' >&2
  echo "  (synthetic test values belong in the allowlist in this script and .gitleaks.toml; install gitleaks for a real scan)" >&2
  exit 1
fi
echo "  secret sweep (grep fallback — install gitleaks for a real scan): clean"
