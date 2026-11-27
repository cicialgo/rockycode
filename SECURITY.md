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
- **Host file scopes** — read_file, grep, glob and image inputs share a bounded
  default: workdir, explicit `--allow-dir` roots and human read grants. Local
  TUI/desktop YOLO deliberately permits ordinary reads across the filesystem
  accessible to that process. Tightening the mode revokes the wide reads and
  session read grants. Search checks resolved symlink targets before traversal;
  known secret-file protection remains in every mode. Project configuration
  cannot opt into the local scope. Structured writes remain confined to workdir
  and explicit extra roots; read approval never adds a write root. Arbitrary
  shell/plugin execution is outside this file-tool policy, and resolve/open
  checks do not fully defend against malicious concurrent filesystem changes.
- **Trusted image routing** — view_image ignores project image CLI routing;
  explicitly configured user backends remain available and may start a process.
  A read profile means no general shell tool, not a process sandbox.
- **Secret redaction** — known credentials are scrubbed from tool output.
  Raw conversation/trajectory records and every event path must still be treated
  as sensitive; redaction is not a credential or tenant isolation boundary.
- **Where secrets live** — the CLI keeps your key in `~/.rockycode/.env` (created
  `0600`, in a `0700` dir) or, with the `[keyring]` extra, the OS keychain; the
  VS Code extension uses encrypted **Secret Storage**. Never in synced settings.
- **Keys stay out of the tools' environment** — the shell tool, the code checks,
  and MCP and LSP servers start with your environment minus everything rocky
  loaded from its store (`~/.rockycode/.env`, the keychain) and minus rocky's own
  key variables (`ROCKYCODE_…_API_KEY`), even one you exported yourself. A
  command, a test runner or a dependency that dumps the environment finds no
  rocky key in it. Your own exports (`GITHUB_TOKEN`, a project's keys) still
  reach the shell tool, because it has to run your project.
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

- **`yolo` automates tool approval within the active deployment.** Static blocks
  and secret-file protection remain. It does not answer user-input requests,
  choose a destination session, or turn a paste into permission to send.
- **The permission layer is advisory.** For strong isolation on genuinely
  untrusted code, run inside the Docker **sandbox** (`--sandbox`) or use `goal`
  mode's worktree copy — not a bare `chat`.
- **The local shell tool runs as you.** Its environment carries no rocky key, but
  an approved command can still open any file your account can, the store file
  included. Only the sandbox takes that away.
- **The decision verifier is not a security boundary.** It is off unless you opt in,
  and a project config cannot turn it on. Static block rules still win.
  Timeouts and bad answers fail closed (ask or deny, never silent allow).
  The command summary and a short task snippet are sent to the selected
  provider (Jev or Ollama) when enabled. Explicit shadow observation can also
  record YOLO calls without changing them. Local option scores are not
  calibrated confidence.
- **Redaction is best-effort.** It can't match every possible secret shape; don't
  rely on it as your only protection against leaking a token.
- **Hosted isolation must surround the whole worker.** Run Engine, clients,
  extensions and task state inside a tenant/task sandbox. A shared scheduler
  should have no workspace mounts or generic file tools. The full profile's
  inner Docker network switch only covers that tool container, not the outer
  model clients or host processes. Do not isolate tenants by switching process
  environment variables or trusting user-supplied paths/registry entries.

## Guidance for running untrusted code

- Reviewing a repo you don't trust? Use `--sandbox`, keep the permission mode at
  `ask` or `careful`, and don't pass `--yolo`.
- On a shared machine, store your key in the OS keychain:
  `pip install 'rockycode[keyring]'`.
- The biggest real attack surface for the project's website is **account
  security** (registrar, GitHub, host), not the static site — keep those on 2FA.
