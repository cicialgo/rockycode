# Security Policy

## Reporting a vulnerability

**Please don't open a public issue for security bugs.** Report it privately
through GitHub's **Security → Report a vulnerability** button on this repo. That's
GitHub's Private Vulnerability Reporting — the report is visible only to the
maintainers until a fix ships, so there's no public exposure and no email to
send. We'll acknowledge within a few days and keep you posted through the fix.
Responsible disclosure is appreciated; credit is given unless you'd rather stay
anonymous.

## Supported versions

rockycode is pre-1.0. Security fixes land on the current `0.0.x` line; there is
no back-porting to older tags yet.

## Threat model — assume hostile clones

rockycode is built to run inside repositories you don't fully trust. **The cloned
repo, its `.rockycode/` config, its `.venv`, its `.mcp.json`, and the model's own
output are all treated as potentially attacker-controlled.** The question we
design against is: *what can a malicious repo or a compromised model do to the
person running `rockycode` in it?*

### What's defended

- **Opt-in permission layer** — risky tools (shell, network, file writes) prompt
  before running. A project's `.rockycode/config.toml` can only *tighten* the
  approval mode, never loosen it, and the CLI flags a project that tried.
- **Workdir path jail** — the structured file tools (read / write / edit)
  hard-refuse any path outside the working directory — `..`, absolute paths, and
  symlinks that point out are all caught — and refuse known secret files
  (`.env`, `~/.ssh`, keys) even inside it. This holds in *every* mode (yolo /
  bench / serve included); it's enforced in the tool, below the advisory layer.
  Widen it only with an explicit `--allow-dir` at launch — a project's own config
  can never widen its jail. Anything further goes through the gated bash tool.
- **Secret redaction** — API keys and known env secrets are scrubbed from tool
  output, history, and trajectory logs at a single chokepoint before they can
  reach the model or disk.
- **Where secrets live** — the CLI keeps your key in `~/.rockycode/.env` (created
  `0600`, in a `0700` dir) or, with the `[keyring]` extra, the OS keychain; the
  VS Code extension uses encrypted **Secret Storage**. Never in synced settings.
- **Network (SSRF) allowlist** — the web tools validate URLs against an allowlist
  and re-validate on every redirect hop.
- **Untrusted MCP** — a project `.mcp.json` is **not** auto-started (it needs an
  explicit env opt-in); tool-description poisoning is scanned and blocked/warned;
  spawned MCP servers get a secret-stripped environment.
- **Isolation for autonomous mode** — `rockycode goal` works on an isolated
  git-worktree **copy** of your repo, inside a Docker sandbox, under a hard
  budget cap and a safety classifier — so an overnight run can't wreck the
  original tree.
- **No unsafe deserialization** — config is `tomllib`, data is `json`/markdown.
  No `pickle`, `yaml.load`, `eval`, or `exec` anywhere in the agent path.

### What is *not* a full defense — know your risk

- **`yolo` mode disables the prompts.** Only use it in a repo you trust.
- **The permission layer is advisory.** For strong isolation on genuinely
  untrusted code, run inside the Docker **sandbox** (`--sandbox`) or use `goal`
  mode's worktree copy — not a bare `chat`.
- **Redaction is best-effort.** It can't match every possible secret shape; don't
  rely on it as your only protection against leaking a token.

## Guidance for running untrusted code

- Reviewing a repo you don't trust? Use `--sandbox`, keep the permission mode at
  `ask` or `careful`, and don't pass `--yolo`.
- On a shared machine, store your key in the OS keychain:
  `pip install 'rockycode[keyring]'`.
- The biggest real attack surface for the project's website is **account
  security** (registrar, GitHub, host), not the static site — keep those on 2FA.
