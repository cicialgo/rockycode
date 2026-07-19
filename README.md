<div align="center">

<img src="brand/rockycode-wordmark.svg" alt="rockycode" width="420">

<br>

**A coding agent engine you can talk to**<br>
Built for the DeepSeek V4 series, with a unique research mode, bench-tested, and self-evolving features under development.

[English](README.md) · [简体中文](README.zh-CN.md)

![SWE-bench Verified](https://img.shields.io/badge/SWE--bench_Verified-~80%25_100--task_slice-7d5cc6)
![Python](https://img.shields.io/badge/Python-3.11%2B-9d7cd8)
![License](https://img.shields.io/badge/License-MIT-a9b1d6)

</div>

---

rockycode is a coding-agent harness, adapted for the DeepSeek V4 series and
able to run on any OpenAI-compatible endpoint. Leaning on DeepSeek V4's broad
world knowledge, it adds a search-augmented **Research mode**; it keeps the
core lean while exploring how to wire the harness onto Docker-based benchmarks,
so that every change to the framework produces a change in the score — a
number, not an impression; and it wraps a Docker sandbox around the risky
operations `goal` and `exec` modes might run. We also ship a batch of
still-rough **experimental features** exploring the harness's self-evolution —
and how the trajectories it produces feed better post-training, so model and
framework improve together.

**A single engine powers three entry points:**

| Entry point | Command | What it does |
|---|---|---|
| **Interactive agent** | `rockycode` | A terminal UI where Rocky reads, edits, and runs code in your project with native tool calls, streaming his reasoning as he works. |
| **Autonomous runner** | `rockycode goal` | Executes an objective unattended — on an isolated git-worktree **copy** of your repository, inside a Docker sandbox, under a hard budget cap, driving a plan → verify → review loop. The result is a git branch for you to review. |
| **Measurement rig** | `rockycode bench` | Drops the same agent loop onto SWE-bench Verified and scores it with the official harness, so every prompt or loop change is measured rather than felt. |

Every session — interactive or benchmarked — is logged as a training-ready
trajectory. The harness therefore doubles as an **RL environment**: the
groundwork for fine-tuning small models (tool use, compaction, memory roles)
against it.

## Results — SWE-bench Verified

On SWE-bench Verified, a **randomly-chosen 100-task slice** (resources are
limited — no repeated runs averaged over the full 500):

**Result: ≈80% on a 100-task SWE-bench Verified slice** with `deepseek-v4-pro`,
no tuning against those tasks — the mean of a four-arm configuration sweep.

Read it with its limits. It's a representative **random 100-task slice**, not
the official 500-task Verified set: its harder 20-task core scored 60–70% while
the other 80 scored >80%. Treat it as an honest internal measurement, not a
leaderboard entry; for the full breakdown, follow our X
([@rockycode_ai](https://x.com/rockycode_ai)).

We plan to add **DeepSWE-bench** support as well — currently in progress.

## Getting started

Requirements: Python 3.11+ via [uv](https://docs.astral.sh/uv/) and an
OpenAI-compatible API key (DeepSeek is the default provider). That is the
entire install for chat and the research/learn modes — no Docker.

**Docker Desktop** is required only for the modes that isolate tool execution
in a container: `goal` (autonomous runs), `exec` (headless delegation),
`bench` (SWE-bench scoring), and the optional `/sandbox` in chat. Those modes
run offline in the sandbox by design, so a delegated or unattended task cannot
touch your host or reach the network.

```bash
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv tool install .      # puts the `rockycode` command on your PATH
rockycode              # the first run walks you through API-key setup
```

On first launch you paste your API key once. It is stored in the OS keychain
(with the `[keyring]` extra) or in a private `0600` file at
`~/.rockycode/.env` — never in your project and never in your shell profile.
rockycode never reads a project `.env`: a cloned repository must not be able
to supply a key or redirect the endpoint, so credential-shaped variables in
one are warned about by name with their values left unread.

Run it in any project:

```bash
rockycode                        # current directory
rockycode -C ~/code/myproject    # any other project
rockycode -r                     # browse past sessions and pick one (or -r <id> for a specific one)
```

Working on rockycode itself? `uv sync && uv run rockycode` runs straight from
the clone, no install.

### Terminal setup

The TUI is fully mouse-driven: wheel-scroll the history, click file links,
dock a document beside the chat, and drag over any text to copy it (release =
copied, a toast confirms).

| Terminal | Setup |
|---|---|
| **ghostty** | None — mouse and clipboard work out of the box. |
| **iTerm2** | Enable **Settings → Profiles → Terminal → "Enable mouse reporting"**; without it, scrolling, clicks, and drag-copy never reach the app. `⌥`-drag keeps iTerm2's native selection. The clipboard needs no settings (copy goes through `pbcopy`). |
| **VS Code terminal** | None — mouse events are on by default. |

Over SSH the clipboard rides OSC 52 — enable "applications may access
clipboard" (or your terminal's equivalent) on the local end.

## Interactive use

### Slash commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/plan [topic\|off]` | Plan mode: read-only exploration into a plan you approve, then build it here or hand it to `goal` |
| `/goal [objective]` | Go autonomous in its own screen (requires Docker) |
| `/research` | Research modes: deep-research · paper-reading · whiteboard · prove |
| `/learn` | Tutor mode — your understanding is the goal, not the diff |
| `/model` | Switch provider and model (see below) |
| `/effort off\|high\|xhigh\|max` | Reasoning depth, adjustable live per session |
| `/permission yolo\|ask\|careful` | Tool-approval strictness for the session |
| `/sandbox on\|off\|status` | Isolate tool execution in a container |
| `/lsp` | Language-server status; diagnostics ride along with `read_file` |
| `/artifact live on\|off` | Auto-refresh HTML artifacts in the browser |
| `/prompt` | Inspect the live system prompt |
| `/mcp` | Connected MCP servers and their tools |
| `/skills` | Installed skills |
| `/memory` · `/remember <note>` | Inspect memory · save a note |
| `/proposals` | Review skills drafted by the dream pass (approve or archive) |
| `/routines` | Run or lease recurring routines |
| `/config [key] [value]` | Show or set preferences |
| `/clear` · `/exit` | Session control |
| `! <cmd>` | Run a shell command directly; the output lands in Rocky's context |

### Modes

- **Plan mode** (`/plan`) — the session becomes read-only except for one plan
  file. Rocky explores the codebase, drafts a plan, and nothing is built until
  you approve it — at which point you can execute it in-session or hand it to
  goal mode.
- **Research modes** (`/research`) — a picker of prompt contracts:
  **deep-research** (multi-source, fact-checked reports), **paper-reading**,
  **whiteboard** (thinking out loud together), and **prove** — which turns an
  informal mathematical claim into a Lean 4 compiler-certified verdict via the
  built-in `lean-prover` skill (Mathlib and TorchLean). *(prove is
  [experimental](#experimental).)*
- **Learn mode** (`/learn`) — a tutor posture: explanations and checks of your
  understanding instead of code dumps.

### Models and providers

DeepSeek is the home model, but providers are data, not code: each is a
base URL, a model list, a key variable, and a reasoning shape over the
OpenAI-compatible API.

| Provider | Models |
|---|---|
| **deepseek** (default) | `deepseek-v4-pro`, `deepseek-v4-flash` |
| **minimax** | `minimax-m3` |
| **kimi** | `kimi-k3` |
| **glm** | `glm-5.2` |

Regional endpoints are addressable as `<provider>-<region>` (e.g. `kimi-cn`),
and custom providers — including local vLLM/SGLang servers — go in
`~/.rockycode/providers.toml`. The `/model` picker only offers providers whose
keys are actually configured. Only DeepSeek is verified on the harness; the
others are [experimental](#experimental).

The effort dial (`/effort off|high|xhigh|max`) is provider-neutral; each
provider maps it to its own reasoning tiers at the wire (DeepSeek, for
example, only distinguishes `high|max`, so `xhigh` clamps to `max`).

## Autonomous use

### Goal mode

`rockycode goal "<objective>"` runs Rocky unattended and hands you a git
branch to review. Safety is structural, not hopeful:

- The run happens on a git-worktree **copy** of your repository — nothing it
  does touches your working tree.
- Tool execution is confined to the Docker sandbox, offline by design.
- Every bash command is screened by a classifier: destructive commands
  (`rm -rf` of a root, `mkfs`, …) are refused outright; risky-but-legitimate
  ones (`git push`, `sudo`, installs) require one up-front approval.
- A **budget cap** — spend, wallclock, and tokens, at real DeepSeek prices
  including the peak-hour surcharge — stops the run gracefully, and the
  worst-case spend is printed *before* the run starts.

The loop plans the objective into milestones, verifies each one with your own
linters (`check_code`), and a periodic reviewer re-plans to keep it on track.
Start small and cheap:

```bash
rockycode goal "add a docstring to <fn> and run the linter" --max-usd 0.50 --max-hours 1
```

### Headless delegation: `exec`

`rockycode exec "<task>"` is the single-shot, non-interactive entry point,
designed to be called by *other* agents and scripts. Events stream as JSONL on
stdout; the Docker sandbox is **on by default** (the command classifier is
defense-in-depth, not the boundary); budgets are always enforced; and exit
codes distinguish success, failure, needs-approval, and budget-stop — so a
calling agent can grant an approval and resume instead of guessing.

### Editor integration: `serve` and the VS Code extension

`rockycode serve` exposes the engine as JSON-RPC 2.0 over stdio, keeping it
UI-agnostic. The bundled VS Code extension (`rockycode-vscode/`) builds on it:
a sidebar chat with streaming reasoning, inline tool-approval cards, and diff
previews — with the API key kept in VS Code's encrypted Secret Storage.

## Memory *(experimental)*

Rocky remembers across sessions in plain markdown files under
`.rockycode/memory/` (facts / skills / episodes / feedback) — the files are
the truth; edit them freely. `MEMORY.md` and user feedback load into every
session; everything else gets a one-line index and is fetched on demand via
the `recall_memory` tool — by exact name or by meaning. Semantic search runs
on local Ollama embeddings (`nomic-embed-text` for English,
`qwen3-embedding:0.6b` for Chinese and cross-lingual) over a self-rebuilding
sqlite-vec + FTS5 index; without Ollama it degrades cleanly to keyword search.
Removal archives, never deletes. Inspect from the shell with
`rockycode memory list|show|search|reindex|edit|rm`; disable with
`--no-memory`.

> ⚠️ In testing we found `/memory` lets remembered context *dominate* every
> session in a project — it can bend a perfectly normal task to fit old
> patterns. We don't recommend it for everyday use yet.

## Experimental

These features work today but are early — opt-in, and their surface may still
change. Anything that could act on its own is **off by default**.

- **Dream** *(early, lightly tested).* `rockycode dream` consolidates recent
  sessions while you rest: a local Ollama model (default `qwen3.5:2b`, zero API
  tokens) digests each trajectory into an episode note, reconciles new facts
  against old ones (contradictions archived, never deleted), rewrites the
  dream-owned section of `MEMORY.md`, and re-embeds the index. `--dry-run`
  previews every decision. Still early — not recommended for everyday use yet.
- **Self-improvement** *(default off).* On top of consolidation, the dream pass
  judges each session into an outcome record, mines recurring failures into
  weakness notes, and drafts candidate skills — and, from tasks you repeat by
  hand, candidate **routines** — into a proposals inbox. Nothing self-installs:
  you approve or archive via `/proposals`; an approved **routine** (`/routines`)
  runs pre-approved and budgeted, on a bounded lease that expires back to
  click-to-run. Enable it with `exit_sheet` / `dream` in config; it stays
  invisible without a local Ollama stack regardless.
- **Formal proof** — `/research prove` and the built-in `lean-prover` skill turn
  an informal math or model claim into a Lean 4 compiler-certified verdict
  (green / amber / red), over Mathlib and TorchLean. The compiler is the judge,
  so "proved" always means a real green build. Tested but still being polished —
  and enabling it pulls a large Lean 4 toolchain download.
- **`explore` — read-only delegation.** Chat can buy a bounded, read-only
  investigation from a fresh-context child that returns only a cited,
  mechanically-verified report; the search noise never enters your session. It
  also grounds goal mode's branch review and milestone verification.
- **Providers beyond DeepSeek.** MiniMax, GLM / z.ai, and Kimi are wired as
  OpenAI-compatible profiles (`/model`), but only DeepSeek is verified on the
  harness — treat the others as untested until they carry a bench number.

## Works with your existing setup

Chat reads what other agents already use; there is no migration step:

- **MCP servers** from the project's `.mcp.json`, Claude Code's user config,
  Claude Desktop's config, and Codex's `~/.codex/config.toml` (stdio servers;
  first-defined name wins, project first). Their tools join Rocky's as
  `mcp__<server>__<tool>`. Disable with `--no-mcp`.
- **Skills** from `.claude/skills/`, `.rockycode/skills/`,
  `~/.claude/skills/` (SKILL.md folders) and `~/.codex/prompts/` (`*.md`).
  Only name and description enter the context; the full instructions load on
  demand via the `skill` tool. Disable with `--no-skills`.
- **Project instructions** in `CLAUDE.md` or `AGENTS.md`, folded into the
  system prompt automatically.

None of this — memory included — loads in `bench`: published scores measure
the harness, not your plugins, and cross-task memory would contaminate
SWE-bench results.

## Security model

rockycode assumes a repository you just cloned might be hostile:

- A project `.mcp.json` is **not** auto-started — a cloned repo cannot run
  code or exfiltrate keys on launch (opt in with
  `ROCKYCODE_TRUST_PROJECT_MCP=1`). MCP tool descriptions are scanned for
  prompt injection.
- `read_file` refuses `.env`, credentials, and private keys; reads outside
  the working directory require approval; a path jail applies regardless of
  permission mode.
- Secrets are redacted from tool output before it reaches the model or the
  trajectory log.
- An untrusted project config can *tighten* the tool-approval mode but never
  weaken it.
- Permission modes (`yolo|ask|careful`) compose with per-command
  classification: block-tier commands are refused even in yolo, and session
  grants are scoped to a single binary.
- Goal mode adds worktree-copy isolation and the bash classifier on top;
  `exec` keeps the sandbox on by default.

Reporting: see [SECURITY.md](SECURITY.md).

## Benchmarking

Benchmarking runs from the clone and needs the `bench` extra (the SWE-bench
harness and the Docker SDK): `uv tool install '.[bench]'` — or
`uv sync --extra bench` and prefix commands with `uv run`.

```bash
# raw single-shot baseline (Docker is only needed for scoring)
rockycode bench --runner raw --tasks dev10

# the harness: Rocky works inside each task's official SWE-bench container
rockycode bench --runner rockycode --tasks dev10

# fast sanity check
rockycode bench --runner rockycode --tasks dev10 --limit 1
```

Useful flags: `--model`, `--limit`, `--skip-score`, `--run-id`,
`--thinking/--no-thinking`, `--reasoning-effort high|xhigh|max`,
`--max-tokens`, `--context-window` (compaction trigger point), `--max-steps`,
and `--prompt <file>` for system-prompt A/B runs (see `prompts/README.md`).

First runs are slow: the HF dataset downloads once (hundreds of MB), and each
task pulls its official image from Docker Hub (~1 GB each, cached forever
after). On Apple Silicon, enable *"Use Rosetta for x86_64/amd64 emulation"*
in Docker Desktop — the images are x86.

## Architecture

- **Engine** (`rockycode/engine/`) — a model- and UI-agnostic ReAct loop. It
  streams the provider with native tool calling, executes tools, and repeats
  until the model answers without them, emitting a typed event stream — the
  TUI, the bench console, the JSON-RPC server, and the trajectory logger are
  all just subscribers. A configurable step cap (`--max-steps`) with budget
  warnings near the end nudges the agent to commit to a fix instead of
  exploring to exhaustion.
- **Compaction** (`engine/compaction.py`) — before every API call the engine
  projects the next prompt size (the last real `prompt_tokens` plus
  conservative estimates for newer messages). At 50% of the context window a
  one-time nudge appears; at 90% it auto-compacts: first stubbing old tool
  outputs (free and deterministic), then — if that is not enough — one API
  call folds the older history into a dense state document and the context is
  rebuilt as `[system, state, recent tail]`. Compactions are events and
  trajectory records, so long tasks survive the window and the rewrite stays
  visible in the training data.
- **Tools** — `bash`, `read_file`, `write_file`, `edit_file`, `grep`, `glob`,
  and `check_code` (the project's own ruff/pyright, or a bundled pyflakes
  fallback, for grounded lint and type feedback); plus `explore` (a read-only,
  citation-verified sub-investigation), web tools, memory tools, and
  goal-branch review tools. The same schemas serve chat and bench; only the
  execution target differs (local directory vs. `docker exec` into the task
  container). Read-only tool batches run concurrently; anything that writes
  stays serial. Tool outputs are written to teach recovery: errors come back
  as readable text, never exceptions.
- **Bench runner** — per task: pull the official image → start a container →
  the agent works at `/testbed` → `git add -A && git diff --cached` is the
  prediction → scored by `swebench.harness.run_evaluation`. Agent and scorer
  share the same images.
- **Trajectories** — every session (chat and bench) appends to
  `.rockycode/trajectories/*.jsonl`: metadata (model, prompt name and sha,
  instance id), every message in OpenAI shape, per-call usage (including
  DeepSeek cache hit/miss), and an outcome record. SFT/RL-ready by design.

### Layout

```
rockycode/
├── rockycode/
│   ├── cli.py               # subcommands: chat/exec/goal/bench/serve/dream/memory/config/pricing
│   ├── engine/
│   │   ├── loop.py          # the ReAct loop (events out, history in)
│   │   ├── providers.py     # provider registry (DeepSeek, MiniMax, Kimi, GLM, …)
│   │   ├── effort.py        # the off/high/xhigh/max dial → provider tiers
│   │   ├── compaction.py    # context compaction (prune → state summary)
│   │   ├── tools.py         # tool schemas + local execution + path jail
│   │   ├── permission.py    # yolo/ask/careful × risk tiers → allow/ask/block
│   │   ├── planmode.py      # the read-only plan-mode gate
│   │   ├── modes.py         # research/learn mode contracts
│   │   ├── goal.py          # autonomous planner + runner
│   │   ├── headless.py      # `exec`: sandboxed one-shot for other agents
│   │   ├── mcp.py           # MCP client (stdio servers → extra tools)
│   │   ├── skills.py        # skill discovery + progressive disclosure
│   │   ├── web.py           # web_search / web_research / web_fetch
│   │   ├── container.py     # docker-exec execution + patch extraction
│   │   ├── events.py        # the event contract all UIs subscribe to
│   │   └── trajectory.py    # training-ready session logs
│   ├── memory/              # files-as-truth memory store + semantic recall
│   ├── dream/               # consolidation, session judge, mining, proposals
│   ├── routines.py          # recurring pre-approved work (leased auto-runs)
│   ├── modes/               # research/learn mode contracts (markdown)
│   ├── skills/              # built-in skills (lean-prover)
│   ├── tui/                 # Textual chat app (rocky theme)
│   ├── runners/             # raw baseline · agent-on-SWE-bench · shared data
│   ├── prompts/rocky.py     # built-in system + task prompts
│   └── score.py             # wraps the official swebench eval
├── rockycode-vscode/        # VS Code extension (chat panel over `rockycode serve`)
├── prompts/                 # prompt lab (A/B variants)
├── bench/tasks/dev10.json   # the fast iteration subset
└── tests/                   # docker-free smoke tests (fake model streams)
```

## Prompt lab

System prompts are swappable files. Copy `prompts/rocky-v1.txt`, change one
thing, run dev10 with `--prompt`, and compare score, steps, and tokens. Names
and hashes are recorded everywhere, and per-variant prediction files never
clobber each other. Details in `prompts/README.md`.

## Development

The smoke suite needs no API key and no Docker — a scripted fake model stream
drives the engine end to end:

```bash
uv run python tests/run_all.py           # the whole gate (what CI runs)
uv run python tests/run_all.py --all     # include the Docker-dependent tests
uv run python tests/smoke_engine.py      # or any single piece by name
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions and
[CHANGELOG.md](CHANGELOG.md) for release history.

## About the name

> *"i learn traditional physics. i no know e=mc^2 yet. but we fix bug. amaze!"*

- **rockycode** — Rocky from *Project Hail Mary*: enthusiastic, curious,
  occasionally wrong, gets there anyway. He sings ♪♫ while he thinks.
- **dev10** — the fast 10-task iteration subset; real runs use full Verified.
- **amaze** — what Rocky says when the tests pass.

## License

[MIT](LICENSE)

## Contribution

Contributions are welcome — but the project is still early and has plenty of
rough edges, so we'd rather talk through feature and architecture design with
you before diving in. We're not trying to make it big-and-comprehensive: we
picture it as a modified N-1 starfighter — pushed to the limit in a few places,
and that's enough.

The initial commit comes from these developers:  
[@cicialgo](https://github.com/cicialgo) — LLM algorithm engineer; overall design and adding the stranger experimental features.  
[@dy2012](https://github.com/dy2012) — LLM engineer and architect; the permission, security, and Docker-based protections, the VS Code extension, coding-capability improvements, and many fixes.  
[@codingmiu](https://github.com/codingmiu) — ML researcher; the standout research-mode designs.
