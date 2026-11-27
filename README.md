<div align="center">

<img src="https://raw.githubusercontent.com/cicialgo/rockycode/main/brand/rockycode-wordmark.svg" alt="rockycode" width="420">

<br>

**An agent you build with, for coding and science**<br>
A lean engine with native support for many models. One engine for the terminal and the desktop app.<br>
Every change can be measured: SWE-bench Verified is supported, DeepSWE is in progress, more long-horizon tasks are planned.

[English](https://github.com/cicialgo/rockycode/blob/main/README.md) · [简体中文](https://github.com/cicialgo/rockycode/blob/main/README.zh-CN.md)

![SWE-bench Verified](https://img.shields.io/badge/SWE--bench_Verified-88.8%25_V4--flash_GA-7d5cc6)
![V4-pro preview](https://img.shields.io/badge/V4--pro_preview-74.9%25-8d6cd0)
![Python](https://img.shields.io/badge/Python-3.11%2B-9d7cd8)
![License](https://img.shields.io/badge/License-MIT-a9b1d6)

</div>

---

RockyCode is an agent harness you build with, for coding and scientific work.
You set the direction. Rocky reads, edits and runs code, searches the web,
reads papers and checks proofs, and streams the reasoning while the work
happens. It is natively adapted to several model families — the DeepSeek V4
series, the Kimi K3 series, GLM, MiniMax, MiMo and more — and it runs on any
OpenAI-compatible endpoint.

RockyCode is also a **harness benchmark**. The loop that works in your
repository is the loop that takes the benchmark. Keep the model fixed, change
one prompt or one piece of loop logic, and the change in score is the effect
of that change.

| Benchmark | Status |
|---|---|
| **SWE-bench Verified** | Supported: the full 500 tasks, scored with the official harness ([results](#results--swe-bench-verified)) |
| **DeepSWE** | In progress |
| **More long-horizon tasks** | Planned |

**What makes Rocky unique:**

- **Build together** — `/plan` explores first, discusses and revises the plan
  with you, and builds after you approve. Whiteboard mode thinks a problem
  through with you. A step that needs your decision becomes an approval card,
  and in `yolo` the steps only you can allow wait for you in `/parked`.
- **Science** — Research mode: Deep Research writes multi-source,
  fact-checked reports, paper-reading goes through a paper with you, and
  `prove` hands a math claim to the Lean 4 compiler to check
  *(experimental)*. The built-in `architecture-viz` skill shows a neural-net
  block three linked ways: the paper diagram, the tensor shapes and your
  PyTorch code. Lean 4 is the first scientific tool in Rocky. More are
  planned.
- **Many models, and a race to choose one** — each model's own reasoning
  parameters, context limits, vision support, prices and subscription plans
  live in one data file (`models.toml`), and the engine calls every model in
  that model's own way. Model Race in the desktop app runs five models on one
  task in anonymous lanes A–E. The model names appear after you save your
  scores (or click reveal), and a finish board replays the real finishing
  order. You choose a model on your own task.
- **Desktop app** *(preview)* — a window over the same Python core: pick a
  folder, add a provider key in the app, run tasks with streaming output and
  inline approvals, keep notes per task, preview HTML results. The interface
  speaks English and Chinese. See [Desktop app](#desktop-app-preview).
- **Search augmentation** — search and page reading run on the model
  provider's servers (DeepSeek, or MiniMax when you select it). Your machine
  only needs to reach the model API, so search works on a restricted network
  too. `web_research` sends several queries in parallel. Bing and DuckDuckGo
  are the keyless backup.
- **Docker mode** — unattended work happens in a sandbox. `goal` runs on a
  git-worktree copy of your repository: inside a Docker sandbox, offline,
  under a budget cap, and it hands you a git branch to review. `exec`, the
  entry point other agents call, runs shell commands in the sandbox by
  default. In chat, `/sandbox` turns the sandbox on at any time.
- **Agents that talk across sessions** — Rocky in one task can hand work off
  to a new task, or leave a message in another task's inbox (`/task`). The
  message waits there as reference for that task, and a ✉ under the title
  counts what is waiting.

We also ship a batch of still-rough **experimental features** exploring the
harness's self-evolution — and how the trajectories it produces feed better
post-training, so model and framework improve together.

**One engine, several entry points:**

| Entry point | Command | What it does |
|---|---|---|
| **Terminal** | `rockycode` | A terminal UI where Rocky reads, edits, and runs code in your project with native tool calls, streaming his reasoning as he works. |
| **Desktop app** *(preview)* | `apps/desktop`, run from source | A window over the same Python core: pick a folder, run tasks with streaming output and inline approvals, keep per-task notes, preview HTML results, and race five models on one task. It runs from source today. |
| **Autonomous runner** | `rockycode goal` | Executes an objective unattended — on an isolated git-worktree **copy** of your repository, inside a Docker sandbox, under a hard budget cap, driving a plan → verify → review loop. The result is a git branch for you to review. |
| **Harness benchmark** | `rockycode bench` | Puts the same agent loop on SWE-bench Verified and scores it with the official harness. Every change to a prompt or to the loop can be measured. |

There is also **`exec`**: other agents can call rockycode and hand it a task,
for automated pipelines. Its shell commands run inside the Docker sandbox by
default, one more layer of protection for unattended work.

Every session — interactive or benchmarked — is logged as a training-ready
trajectory. The harness therefore doubles as an **RL environment**: the
groundwork for fine-tuning small models (tool use, compaction, memory roles)
against it.

## Results — SWE-bench Verified

Full-set numbers: **independent full-500 runs**, three rounds per model, same
harness and config for all (100-step cap, 32,768 max output tokens, reasoning
effort `max`, thinking on), scored with the official SWE-bench harness. One prompt and one loop served all 500 tasks. Mind the versions: the `deepseek-v4-flash` column is
the **GA release** (V4-Flash-0731), while the `deepseek-v4-pro` rounds ran
before the GA V4-Pro-0813 shipped — that column is the **preview** pro, so the flash/pro gap includes a version difference.

| run | `deepseek-v4-flash` (GA) | `deepseek-v4-pro` (preview) | `minimax-m3` |
|---|---|---|---|
| round 1 | 90.0% (450/500) | 75.6% (378/500) | 72.8% (364/500) |
| round 2 | 88.6% (443/500) | 74.8% (374/500) | 71.2% (356/500) |
| round 3 | 87.8% (439/500) | 74.4% (372/500) | 70.0% (350/500) |
| **average** | **88.8%** | **74.9%** | **71.3%** |
| union of runs (pass@3) | 95.4% (477/500) | 81.8% (409/500) | 83.6% (418/500) |

Read the two summary rows differently. The **average** is the
leaderboard-comparable number — each round is an independent single-pass run
over the full 500. The **union** is pass@3: tasks solved by at least one
round. The gap between them (~7 points for both DeepSeek models, ~12 for
MiniMax) is run-to-run variance: the models already reach these tasks under this harness, and hold them in some of the runs. Closing that gap, with verify-before-finish gating and run selection, is the current line of work. For reference, DeepSeek reported 80.6% for V4
Pro (Preview) with its own scaffold. Per-round breakdowns:
[@rockycode_ai](https://x.com/rockycode_ai).

Two earlier flash rounds (79.8% — previously listed here — and 81.2%) hit
local network outages mid-run, visible as contiguous blocks of empty-patch
tasks; clean re-runs replaced them.

**Next benchmarks:** DeepSWE support is in progress. More long-horizon tasks
come after it.

## Getting started

Requirements: Python 3.11+ via [uv](https://docs.astral.sh/uv/) and an API
key for one model provider. The first run asks where your models come from:
any provider in the catalog (DeepSeek is the default), a local Ollama (keyless), or your own OpenAI-compatible URL. That is the entire install for chat and the research/learn modes.

**Docker Desktop** is required only for the modes that isolate tool execution
in a container: `goal` (autonomous runs), `exec` (headless delegation),
`bench` (SWE-bench scoring), and the optional `/sandbox` in chat. Those modes
run offline in the sandbox by design, so a delegated or unattended task stays inside the sandbox, away from your host and the network.

```bash
uv tool install rockycode     # recommended — puts the `rockycode` command on your PATH
rockycode                     # the first run walks you through API-key setup
```

Already installed and a new release is out? **`uv tool upgrade rockycode`** —
use `upgrade` for this, because re-running `uv tool install` sees the existing install and keeps the old version.

New to uv? One command installs it:
`curl -LsSf https://astral.sh/uv/install.sh | sh` — Windows and other options
in the [uv install docs](https://docs.astral.sh/uv/getting-started/installation/).

Three ways to install — they look similar but land in different places:

- **`uv tool install rockycode`** (recommended) — gives the CLI its own
  isolated environment and puts `rockycode` on your PATH; if your system
  Python is older than 3.11, uv fetches a matching interpreter by itself.
  The "install it like an app" path.
- **`uv pip install rockycode`** — installs into the **currently active
  virtual environment** only: the `rockycode` command exists inside that
  venv, so a new shell finds it once the venv is active (or run it as `uv run rockycode`).
- **`pip install rockycode`** — same venv caveat as above, and it needs
  Python 3.11+. On an older Python it fails with the misleading
  `ERROR: No matching distribution found for rockycode`. Why: pip only
  offers releases whose `requires-python` matches your interpreter, so an old Python sees zero installable versions and pip reports a missing package. If you hit this, use `uv tool install rockycode` above; uv brings its own Python 3.11+.

Or install from source:

```bash
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv tool install .
```

On first launch you paste your API key once. It is stored in the OS keychain
(built in on macOS and Windows; on Linux, install the `[keyring]` extra), or
in a private `0600` file at `~/.rockycode/.env` when you pick that row. Both
places are outside your project and outside your shell profile.
rockycode reads keys only from its own store. A project `.env` is ignored, which keeps the key and the endpoint in your hands when you open a cloned repository; credential-shaped variables in one get a warning by name, and their values stay unread. What the store holds stays inside rocky: the commands it runs for the model get your shell's environment only. If your own commands need a variable, export it in your shell.

Run it in any project:

```bash
rockycode                        # current directory
rockycode -C ~/code/myproject    # any other project
rockycode -r                     # browse past sessions and pick one (or -r <id> for a specific one)
```

Working on rockycode itself? `uv sync && uv run rockycode` runs straight from the clone.

### Terminal setup

The TUI is fully mouse-driven: wheel-scroll the history, click file links,
dock a document beside the chat, and drag over any text to copy it (release =
copied, a toast confirms).

| Terminal | Setup |
|---|---|
| **ghostty** | Ready as it is: mouse and clipboard work out of the box. |
| **iTerm2** | Enable **Settings → Profiles → Terminal → "Enable mouse reporting"**; it lets scrolling, clicks, and drag-copy reach the app. `⌥`-drag keeps iTerm2's native selection. The clipboard works as it is (copy goes through `pbcopy`). |
| **VS Code terminal** | Ready as it is: mouse events are on by default. |

Over SSH the clipboard rides OSC 52 — enable "applications may access
clipboard" (or your terminal's equivalent) on the local end.

## Interactive use

### Slash commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/plan` | Enter interactive planning using this conversation; discuss and revise, review the full plan, then approve building here or in `goal` |
| `/goal [objective]` | Go autonomous in its own screen (requires Docker) |
| `/task` | Linked tasks: `new <handoff>` · `open <id>` · `list` · `send <id> <message>` · `inbox` · `use <message-id>` · `dismiss <message-id>`. Once a project has two, they sit under the title as clickable notes, `♪ 01 fix login   ♫ 02 tests ✉1`: click one to switch (or type its number, `/task open 2`), `✉` is waiting mail |
| `/loop 5m <prompt>` | Check back every 5 minutes, in this chat. Bare `/loop` lists; `stop` · `pause` · `allow` |
| `/research` | Research modes: deep-research · paper-reading · whiteboard · prove |
| `/learn` | Tutor mode: your understanding is the goal |
| `/model` | Switch provider and model (see below) |
| `/effort off\|low\|high\|max` | Reasoning depth, adjustable live per session (clamped onto each provider's own tiers) |
| `/permission yolo\|ask\|careful` | Tool-approval strictness for the session — bare opens a picker; `shift+tab` cycles it, or click the 🔒 chip in the status bar |
| `/sandbox on\|off\|status` | Isolate tool execution in a container |
| `/lsp` | Language-server bridge (experimental): status and how to set it up. Lint and type checks come from `check_code`, which works by itself |
| `/artifact` | Saved works: a searchable picker for the project or the session, with preview, edit and version history. Each HTML artifact has a stable ID and numbered versions; `import <file>` takes in an older HTML file |
| `/paste` | Attach a clipboard image (or `ctrl+v`); text-only models pick a route |
| `/prompt` | Inspect the live system prompt |
| `/mcp` | Connected MCP servers and their tools |
| `/skills` | Installed skills |
| `/memory` · `/remember <note>` | Inspect memory · save a note |
| `/proposals` · `/routines` | Review skills drafted by the dream pass · run or lease recurring routines (listed while `evolve = auto` or something is waiting) |
| `/config [key] [value]` | Show or set preferences |
| `/queue [on\|off\|resume\|clear]` | A message sent while Rocky works interrupts (`off`, default) or waits its turn (`on`); inspect, resume or clear the waiting ones |
| `/parked` | Steps only you can allow, which `yolo` kept for you; decide them here (listed while something waits) |
| `/clear` · `/exit` | Session control |
| `! <cmd>` | Run a shell command directly; the output lands in Rocky's context |

**Preferences.** `/config ui_language zh` switches the interface to Chinese
(`en` for English; `auto` follows `language`). `/config language` sets the
language Rocky answers in (`auto` mirrors the language you write in).
`/config currency cny` shows cost from each provider's own CNY price table,
and `rockycode pricing` prints the live price table.

### Modes

- **Plan mode** (`/plan`) — the session becomes read-only, with one writable plan file. Rocky explores the codebase and drafts a plan. Building starts when you approve it: in this session, or handed to goal mode.
- **Research modes** (`/research`) — a picker of prompt contracts:
  **deep-research** (multi-source, fact-checked reports), **paper-reading**,
  **whiteboard** (thinking out loud together), and **prove** — which turns an
  informal mathematical claim into a Lean 4 compiler-certified verdict via the
  built-in `lean-prover` skill (Mathlib and TorchLean). *(prove is
  [experimental](#experimental).)*
- **Learn mode** (`/learn`) — a tutor posture: explanations, and checks of your understanding.

### Models and providers

DeepSeek is the home model, and **every model is data**: the whole
catalog lives in `rockycode/models.toml` — per provider a China base URL, a
key name and a reasoning wire shape; per model its context window, output
cap, vision flag, price and roles. The engine reads every model-specific number from that spec, so a new model is a data edit. Your own
`~/.rockycode/models.toml` (same shape) is deep-merged on top: add a model,
correct a limit, add a price, hide a row.

| Provider | Models (❖ = takes image input) | ctx / max out |
|---|---|---|
| **deepseek** (default) | `deepseek-flash` (V4.1 Flash, default) ❖, `deepseek-v4-pro` | 1M / 384K |
| **glm** | `glm-5.3`, `glm-5.3-flash` ❖ | 1M / 128K |
| **kimi** | `kimi-k3` ❖, `kimi-k3.1-pro` ❖ | 1M / 128K |
| **minimax** | `minimax-m3` ❖, `minimax-m3.1-flash` ❖ | 1M / 128K |
| **stepfun** | `step-5-preview` ❖ | 1M / 64K |
| **qwen** | `qwen3.8-max` ❖, `qwen3.8-flash` ❖ | 1M / 64K |
| **qwen-plan** | `qwen4-max` ❖ | 1M / 128K |
| **mimo** | `mimo-v2.6-pro` ❖ | 1M / 128K |
| **openrouter** (free, opt-in) | `openrouter-free` ❖ (Free Models Router) | 64K / 8K |
| **ollama** (local, $0) | whatever you've pulled — discovered live from the running server | server-verified |

OpenRouter is the free path: a free key is enough to start. It is
OpenAI-compatible at `https://openrouter.ai/api/v1`. Set
`ROCKYCODE_OPENROUTER_API_KEY` (a key from https://openrouter.ai/keys). That name is rocky's own; the verifier's `OPENROUTER_API_KEY` is a separate key, for the verifier only. DeepSeek stays the launch default. `/model
openrouter` sends `openrouter/free` (a router that picks a free model which
supports the request, including tools; it can land on a very small model).
Free-tier limits (OpenRouter, checked 2026-10-23): 20 requests/minute and
50/day until the account has bought at least $10 of credits; 1,000/day after
that. Prices are omitted. Requests go out with the reasoning fields left off (`reasoning = none`). Rocky clamps the context to the smallest tool-capable `:free` window that day (64K / 8K); the router's published ceiling is 200K.
Groq (`https://api.groq.com/openai/v1`) and Gemini's OpenAI-compatible
endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) are yours to add in `~/.rockycode/providers.toml`.

One China endpoint per provider (`ROCKYCODE_<PROVIDER>_API_KEY`; the older
`_CN_`/`_EN_` names are still read), except OpenRouter above. Subscription plans with their own URL
and key are their own rows — `qwen-plan` (Bailian Token Plan), `mimo-plan`,
`stepfun-plan` — keyed as `ROCKYCODE_<PROVIDER>_PLAN_API_KEY`. The `/model`
picker lists **models first**, one row each; a model with several endpoints
then asks which URL serves it (official · plan · your own), and the "custom
base URL" row remembers a gateway or proxy per provider
(`~/.rockycode/endpoints.toml`, addressable as `<provider>-custom`). Typed
specs skip all of that: `/model glm:flash`, `/model qwen-plan:qwen3.8-max`.
The retired `deepseek-v4-flash` / `-vision-exp` ids still resolve (to
`deepseek-flash`, exactly as DeepSeek serves them). The picker only offers
providers whose keys are actually configured.

#### Keys and hosts, per provider

The built-in **model providers** use Rocky's own key names. Add or change a key
inside Rocky: `/model` → "add or change a key" in the terminal, or the key form in
the desktop app. Rocky tests the key with one free call, saves it, and uses it right
away. The key goes to the OS keychain where there is one, or to
`~/.rockycode/.env` (`NAME=value`, mode `0600`) when you pick that row. The same list
shows where each key lives, and it can move the keys still in the file to the keychain.
A key exported in your shell under the same name takes precedence. Project `.env` files and an ambient `OPENAI_API_KEY` are ignored. The verifier has a separate key
lookup, described below. Keep keys out of the chat: the config tool rejects key-shaped arguments, and the key field is the place for a secret. If you edit
`~/.rockycode/.env` by hand, restart Rocky so it loads the new names. Each model uses
its own provider's key, so one provider's key is enough to start.

The table lists the **1.0.0 built-in defaults**; a vendor may offer more services.
Most cloud providers default to their China host; OpenRouter is international and
Ollama is local. For an international account, use the matching provider URL below
via the picker's "custom base URL" row or `~/.rockycode/endpoints.toml`, then explicitly
select `<provider>-custom:<model>`. A custom URL uses that provider's primary key. The bare model id keeps its own route, and model ids are sent as you type them.

| Provider | Key name | Built-in base URL | Built-in plan row · its key | International base URL | Console |
|---|---|---|---|---|---|
| **deepseek** | `ROCKYCODE_API_KEY` | `https://api.deepseek.com/v1` | — | same host worldwide | platform.deepseek.com |
| **glm** | `ROCKYCODE_GLM_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` | — | `https://api.z.ai/api/paas/v4` | open.bigmodel.cn · z.ai |
| **kimi** | `ROCKYCODE_KIMI_API_KEY` | `https://api.moonshot.cn/v1` | Kimi Code membership is a separate service, see below | `https://api.moonshot.ai/v1` | platform.moonshot.cn · platform.moonshot.ai |
| **minimax** | `ROCKYCODE_MINIMAX_API_KEY` | `https://api.minimax.cn/v1` | — | `https://api.minimax.io/v1` | platform.minimax.cn · platform.minimax.io |
| **stepfun** | `ROCKYCODE_STEPFUN_API_KEY` | `https://api.stepfun.com/v1` | `stepfun-plan` · `ROCKYCODE_STEPFUN_PLAN_API_KEY` | — | platform.stepfun.com |
| **qwen** | `ROCKYCODE_QWEN_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plan` (Bailian Token Plan) · `ROCKYCODE_QWEN_PLAN_API_KEY` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | bailian.console.aliyun.com |
| **mimo** | `ROCKYCODE_MIMO_API_KEY` | `https://api.xiaomimimo.com/v1` | `mimo-plan` · `ROCKYCODE_MIMO_PLAN_API_KEY` | — | Xiaomi MiMo platform |
| **openrouter** | `ROCKYCODE_OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | — | same host | openrouter.ai/keys |
| **ollama** | keyless | `http://localhost:11434/v1` | — | — | — |

The shipped plan URLs are `https://api.stepfun.com/step_plan/v1` (`stepfun-plan`),
`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` (`qwen-plan`) and
`https://token-plan-cn.xiaomimimo.com/v1` (`mimo-plan`). A dash means the built-in rows stop at the ordinary one; the vendor may still sell a subscription. The StepFun plan URL is still marked "verify on first call" in the registry: the release canary skipped it.

**MiMo Token Plan regions:** a plan key is bound to the cluster that issued it. The
shipped `mimo-plan` row is the China cluster; Singapore is
`https://token-plan-sgp.xiaomimimo.com/v1` and Europe is
`https://token-plan-ams.xiaomimimo.com/v1`, and the other clusters answer
`401 Invalid API Key` (plan keys start with `tp-`, team plans `ttp-`; a `sk-` key is
pay-as-you-go and belongs on the `mimo` row). For a Singapore or Europe key, point the
plan row at your cluster in `~/.rockycode/models.toml` (deep-merged over the shipped
registry; keep your other entries), then `/model mimo-plan:mimo-v2.6-pro`:

```toml
[providers.mimo.plan]
base_url = "https://token-plan-sgp.xiaomimimo.com/v1"
```

The 1.0.0 canary exercised the Singapore cluster this way; the China and Europe clusters still wait for a live check.

**MiniMax international:** store the international key as `ROCKYCODE_MINIMAX_API_KEY`
in Rocky's `.env`, and add or update this entry in `~/.rockycode/endpoints.toml`
(keep your other entries):

```toml
minimax = "https://api.minimax.io/v1"
```

Then use `/model minimax-custom:minimax-m3.1-flash` (wire id `MiniMax-M3.1-Flash`)
or `/model minimax-custom:minimax-m3`. Leave the home `ROCKYCODE_BASE_URL` as it is: changing it would also redirect the home key.

**Qwen Token Plan:** store the plan key as `ROCKYCODE_QWEN_PLAN_API_KEY` and select
`/model qwen-plan:qwen3.8-max` or `/model qwen-plan:qwen3.8-flash`. The endpoint is
already built in. A bare `qwen3.8-max` selects the ordinary `qwen` row with the ordinary key; name the plan row to use the plan key. The Token Plan has its own URL, separate from the ordinary international URL above. Use the URL and service shown in your account's console.

Qwen4 Max runs on the Token Plan: use `/model qwen-plan:qwen4-max`, or launch
with `rockycode chat --model qwen-plan:qwen4-max`. It replaces Qwen4 Max
Preview. A saved `qwen4-max-preview` id resolves to Qwen4 Max, with the same
endpoint and key. The effort tiers are `low`, `high` and `xhigh`, and the
output cap is 131,072 tokens.

The Kimi Code membership (coding plan) is a separate service from the open platform, with its own host and its own model ids. Give it its own row in `~/.rockycode/providers.toml` and pick
`/model kimi-code:k3`:

```toml
[kimi-code]
label = "Kimi Code plan"
base_url = "https://api.kimi.com/coding/v1"   # international: https://api.kimi.ai/coding/v1
key_env = "ROCKYCODE_KIMI_CODE_API_KEY"
models = ["kimi-for-coding", "k3", "k3-256k"]
reasoning = "effort"
thinking_off = false
vision = true
```

Store the membership key as `ROCKYCODE_KIMI_CODE_API_KEY` in Rocky's `.env`.
If it was previously stored as `ROCKYCODE_KIMI_API_KEY`, move that membership key to the new name: the two names are separate. Reserve `ROCKYCODE_KIMI_API_KEY`
for an open-platform key. `kimi-code` is a user-defined row in 1.0.0. `key_env` is whatever name you stored the key under; the row and the `.env`
line only have to agree. The canary exercised `kimi-for-coding`, `k3` and `k3-256k` at
the China coding URL, and the international coding URL accepted the same key; vision still waits for a live test.

**OpenRouter verifier:** Jev has its own key, separate from the model-provider key above.
For an OpenRouter verifier, put its key under `ROCKYCODE_VERIFIER_API_KEY` in Rocky's
`.env` (or export it). Lookup order is `ROCKYCODE_VERIFIER_API_KEY`, then the legacy
`ROCKYCODE_JEV_API_KEY`, then `OPENROUTER_API_KEY`; the last name applies only to the
OpenRouter verifier. Remove a stale higher-priority override before switching services.
The subsequent Jev live replay used an in-process mirror from the model key name to
`OPENROUTER_API_KEY`; in 1.0.0 that mirror is a manual step.

To opt into an OpenRouter verifier, merge this table into `~/.rockycode/config.toml`
(when a `[verifier]` table already exists, update that one), then restart:

```toml
[verifier]
mode = "shadow"
provider = "openrouter"
timeout_ms = 5000
```

Shadow mode sends redacted approval context to the service for observation only; tool approvals stay with you. Verifier settings in the shell or Rocky's `.env`
override this table. The default wire model is `typesafe/jev-1.13`, at
`https://openrouter.ai/api/v1/systemone` (a separate endpoint from the coding model's chat endpoint).

Troubleshooting (one HTTP status can have several causes):

- `401` on the China host → check the key's issuing console, validity and service.
  If it is an international key, configure that service's international URL and
  explicitly pick the `-custom` row. A 401 can also come from the key or the service, so check those first.
- `401` with a subscription key on the ordinary row → verify the subscription and
  key name, then pick its plan row (`qwen-plan:qwen3.8-max`) or `kimi-code` above.
- Rocky stops and names a key variable → add that provider's key with `/model` →
  "add or change a key", or pick a row that already has one.
- The verifier reports `missing_api_key` → use the override above, or the selected
  verifier's `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY` or `PERPLEXITY_API_KEY`.
- Ask Rocky in chat to call `rocky_config` with `action="show"`: it lists all endpoints
  with "key set (NAME)", "key NOT set — NAME" or "no key needed". This is a tool call inside the chat. `/model` uses shorter localized labels and normally shows configured providers only.

These are manual setup instructions for 1.0.0. `rockycode doctor`, `/doctor` and the
new `rockycode key`, `url` and `provider` commands are **planned for 1.1**.

Vision is per-model: `deepseek-flash` sees images on the home key, so the
default session just takes a paste. A text-only model (`deepseek-v4-pro`,
`glm-5.3`) gets pasted images described by `deepseek-flash` silently
(config `image = auto`), or by your own CLI (`image = mmx`). `rockycode config model <spec>`
makes any pick the sticky launch default. Context window and output cap
follow the active model (config `context_window` / `max_tokens` = `0`); a
number pins your own ceiling across switches. DeepSeek and MiniMax carry
full-500 bench numbers (see [Results](#results--swe-bench-verified)); the
other providers are [experimental](#experimental).

The effort dial (`/effort off|low|high|max`) is provider-neutral; each
provider's own tiers come from the registry and the dial is clamped onto
them by position at the wire (StepFun's `low|medium|high` gets `medium` for
rocky's `high`; GLM and Kimi always think, so `off` sends their lowest tier). `xhigh` is still accepted and means `max`.

Rocky can also configure itself: ask it to "use my proxy", "add my vLLM
server", or "switch the default model" and the built-in `rocky-setup` skill
plus the ask-tier `rocky_config` tool make the change under `~/.rockycode`.
Keys stay in your hands: it names the variable and you paste the value.

#### Local models (Ollama)

Rocky integrates the OpenAI-compatible *protocol* and recommends [Ollama](https://ollama.com) as the runtime (its MLX engine covers Apple Silicon since 0.19). Setup is two steps: run `ollama serve`, pull a tool-capable model
(`ollama pull qwen3.8:27b-mlx` is the tested recommendation), and it appears
in `/model` with what's actually pulled, priced `$0 · local`.

Switching to a local model runs a **readiness preflight** first — server up,
model pulled, tool-calling support, serving context — and refuses the switch
with the exact fix (`ollama pull …`, `export OLLAMA_CONTEXT_LENGTH=65536`)
when something would break mid-session. The one to respect: Ollama's default
context is small and it **truncates silently**, which kills agent sessions in
confusing ways — serve with `OLLAMA_CONTEXT_LENGTH=65536`. Rocky paces its own
`context_window` to the server's verified value on every switch.

Other local servers (LM Studio, llama.cpp, vLLM) work as data too: add a
provider with `local = true` in `~/.rockycode/providers.toml` and its
endpoints are keyless.

## Autonomous use

### Goal mode

`rockycode goal "<objective>"` runs Rocky unattended and hands you a git
branch to review. Safety is structural:

- The run happens on a git-worktree **copy** of your repository — your working tree stays as it is.
- Tool execution is confined to the Docker sandbox, offline by design.
- Every bash command is screened by a classifier: destructive commands
  (`rm -rf` of a root, `mkfs`, …) are refused outright; risky-but-legitimate
  ones (`git push`, `sudo`, installs) require one up-front approval.
- A **budget cap** — spend, wallclock, and tokens, priced from each model's
  own published price table (DeepSeek's peak-hour surcharge included) — stops
  the run gracefully, and the worst-case spend is printed *before* the run
  starts.

The loop plans the objective into milestones, verifies each one with your own
linters (`check_code`), and a periodic reviewer re-plans to keep it on track.
Start small and cheap:

```bash
rockycode goal "add a docstring to <fn> and run the linter" --max-usd 0.50 --max-hours 1
```

### Headless delegation: `exec`

`rockycode exec "<task>"` is the single-shot, non-interactive entry point,
designed to be called by *other* agents and scripts. stdout is JSONL: a
`meta` line, the model's `text`, and a `result` envelope with evidence
(files changed, commands run, refusals) for the caller to verify; `--events` adds the per-tool receipt lines. Budgets are always
enforced, and exit codes distinguish success, failure, needs-approval, and
budget-stop. Exit 2 returns `result.blocked_on.grant`, the token a caller
can explicitly grant on a follow-up invocation.

Pick how much rocky may do with `--profile`: `read` (read_file / grep /
glob / view_image only) and `write` (+ write_file / edit_file jailed to `--workdir`) run directly on the host and start instantly — what Claude Code or Codex wants for "look at this repo and
tell me" or a small edit on a cheap, fast model. `full` adds bash, in the
Docker sandbox **by default** (the sandbox is the boundary; the command classifier is defense-in-depth).

```bash
rockycode exec --profile read "which module owns retry logic, and where is it called?"
rockycode exec --profile write "add a docstring to every public function in utils.py"
```

After inspecting an exit-2 receipt, continue with its session id, the grant,
and an **explicit follow-up task** (replace the example id):

```bash
rockycode exec --resume rk_ab12cd34 --allow pkg-install \
  "The earlier edit is complete. Install the blocked dependency, then run the tests, and keep the edit as it is."
```

`--resume` uses the same history-loading mechanism as chat: it creates a **new
session**, carries prior messages/tool results, and runs the follow-up task.
It runs the follow-up task and only that: a retry of the blocked tool, or a redo of the original task, happens when your follow-up asks for it. The follow-up is required; use `-` explicitly to supply it on stdin. The JSONL `meta.resume`
reports `mode: "history-only"`, `requires_prompt: true`, and `from_session`.
The returned session id belongs to the new invocation.

**A resume can repeat work**: the model can issue a new tool call, and the effects of a crashed or interrupted tool may be unknown. Verify prior effects before you request a retry or restart the whole original task. In particular, older trajectories can contain synthetic
`[error] tool execution was interrupted` responses. Docker is recreated, so container-local state starts fresh.

Grants are repeatable (`--allow pkg-install --allow delete`), authorize a category for **this invocation** only; each new invocation states its own.
Unconditional refusals still apply. Profile, model, sandbox/network and
`--allow-dir` settings also come from the current invocation. Resume follows
the project's registered current folder; a conflicting explicit `--workdir`
fails before tools or Docker start. New exec sessions record project identity
so they remain discoverable after a registered folder move.


### Codex MCP: `mcp`

`rockycode mcp` is a stdio Model Context Protocol server so Codex (the app
inside ChatGPT desktop, and the Codex CLI) can call rocky as a tool. `rockycode serve` is a different entry: it speaks rocky's own JSON-RPC for editor clients. The tool is `rocky_exec`, default profile `read` (write or full only when the caller passes that profile). Register
`rockycode mcp` as a stdio server in your MCP client; see the bundled
[example configuration](https://github.com/cicialgo/rockycode/blob/main/integrations/codex-plugin/mcp.json).
The default tool timeout is 60 seconds. Calls can spend model API quota and
send task context and file contents to the configured model provider.


### Editor integration: `serve` and the VS Code extension

`rockycode serve` exposes the engine as JSON-RPC 2.0 over stdio, keeping it
UI-agnostic. The bundled VS Code extension (`rockycode-vscode/`) builds on it:
a sidebar chat with streaming reasoning, inline tool-approval cards, and diff
previews — with the API key kept in VS Code's encrypted Secret Storage.

### Desktop app (preview)

`apps/desktop/` is a window over the same Python core as the terminal. The
agent loop stays in the Python core, so the desktop and the terminal share
one engine, one set of models and one approval policy.

- **Tasks in a folder** — you choose the working folder in the system picker.
  Each task keeps its own draft, notes and model.
- **A key form in the app** — paste a provider key into a hidden field. The
  app tests it with one free request and saves it to the system keychain
  where there is one, or to the 0600 key file.
- **Approvals** — the terminal's policy, per task: `ask`, `careful`, `yolo`.
  Steps only you can allow are kept for you in a list.
- **HTML preview** — HTML results open in a sandboxed panel on the right.
- **Model Race** — five configured models, one task, five isolated runs,
  your scores and an optional anonymous model judge. Export shows a full
  local JSON preview first, and the file stays on your machine.
- **Interface language and theme** — English or Chinese; the **System** theme
  follows your OS, **Nebula** is the terminal's violet-on-night palette.
- **Update reminder** — a banner says when a newer version exists. You
  decide when to download and install.

It runs from source today (Electron + React over the Python core); an
installer comes in a later release. Setup takes a few commands:
[apps/desktop/README.md](https://github.com/cicialgo/rockycode/blob/main/apps/desktop/README.md).

## Memory *(experimental)*

Rocky remembers across sessions in plain markdown files under
`.rockycode/memory/` (facts / skills / episodes / feedback) — the files are
the truth; edit them freely. `MEMORY.md` and user feedback load into every
session; everything else gets a one-line index and is fetched on demand via
the `recall_memory` tool — by exact name or by meaning. Semantic search runs
on local Ollama embeddings (`nomic-embed-text` for English,
`qwen3-embedding:0.6b` for Chinese and cross-lingual) over a self-rebuilding
sqlite-vec + FTS5 index; when Ollama is absent it falls back cleanly to keyword search. Removal moves the file to an archive. Inspect from the shell with
`rockycode memory list|show|search|reindex|edit|rm`; disable with
`--no-memory`.

> ⚠️ In testing we found `/memory` lets remembered context *dominate* every
> session in a project — it can bend a perfectly normal task to fit old
> patterns. For now we recommend it for experiments only.

## Experimental

These features work today but are early — opt-in, and their surface may still
change. Anything that could act on its own is **off by default**.

- **Dream** *(early, lightly tested).* `rockycode dream` consolidates recent
  sessions while you rest: a local Ollama model (default `qwen3.5:2b`, zero API
  tokens) digests each trajectory into an episode note, reconciles new facts
  against old ones (contradictions are archived and kept), rewrites the
  dream-owned section of `MEMORY.md`, and re-embeds the index. `--dry-run`
  previews every decision. Still early: for experiments, for now.
- **Self-improvement** *(default off).* On top of consolidation, the dream pass
  judges each session into an outcome record, mines recurring failures into
  weakness notes, and drafts candidate skills — and, from tasks you repeat by
  hand, candidate **routines** — into a proposals inbox. Every draft waits for you: you approve or archive via `/proposals`; an approved **routine** (`/routines`)
  runs pre-approved and budgeted, on a bounded lease that expires back to
  click-to-run. Off by default since 1.0.0 because every reflect call bills your
  model: `/config evolve auto` turns it on (`evolve = off` stops every call).
- **Formal proof** — `/research prove` and the built-in `lean-prover` skill turn
  an informal math or model claim into a Lean 4 verdict over Mathlib and
  TorchLean: green · compiled, green · kernel-checked (`lake check` on Lean
  v4.35+, Linux bubblewrap), green · paranoid, amber, red, or disputed. Where `lake check` exists, only a kernel-checked result is called proved. Tested but
  still being polished — and enabling it pulls a large Lean 4 toolchain download.
- **`explore` — read-only delegation.** Chat can buy a bounded, read-only
  investigation from a fresh-context child that returns only a cited,
  mechanically-verified report; the search noise stays out of your session. It
  also grounds goal mode's branch review and milestone verification.
- **Bench numbers per model.** GLM, Kimi, MiniMax, StepFun, Qwen, and MiMo
  are registry rows you pick in `/model`. DeepSeek and MiniMax carry full
  bench numbers (see Results); the other rows still wait for theirs. A row marked
  `note = "… verify …"` in `models.toml` (today: the StepFun plan URL) follows
  the provider's docs and still waits for its first live call — a wrong one
  is a one-line data fix.

## Works with your existing setup

Chat reads what other agents already use, as it is:

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

`bench` runs with all of this switched off, memory included: published scores measure the harness alone, and cross-task memory would contaminate SWE-bench results.

## Security model

rockycode assumes a repository you just cloned might be hostile:

- A project `.mcp.json` starts **only after you opt in** (`ROCKYCODE_TRUST_PROJECT_MCP=1`), so a cloned repo stays inert at launch: its code waits, and your keys stay where they are. MCP tool descriptions are scanned for
  prompt injection.
- `read_file` refuses `.env`, credentials, and private keys; reads outside
  the working directory require approval; a path jail applies regardless of
  permission mode.
- Secrets are redacted from tool output before it reaches the model or the
  trajectory log.
- An untrusted project config can only *tighten* the tool-approval mode.
- Permission modes (`yolo|ask|careful`) compose with per-command
  classification: block-tier commands are refused even in yolo, and session
  grants are scoped to a single binary.
- **A change to rocky's own settings always asks you** — the default model,
  a provider's address, a model server — in every approval mode, each time.
  `yolo` keeps such a step for you in `/parked` and goes on with the rest.
- **Every sub-package is pinned.** The package pins its whole dependency
  tree, for the default install and for each extra, to the versions its own
  tests ran with. The desktop app installs from an exact lockfile with
  install scripts off.
- **Keys go to the system keychain** where the machine has one (macOS
  Keychain, Windows Credential Manager, a Linux Secret Service); the key file
  then lists only their names. The 0600 key file is the other choice.
- **Decision verifiers (Jev / Ollama)** are opt-in. Enforce applies only to would-ask calls; shadow can optionally observe YOLO and leaves execution as it is. Static blocks still win.
- Goal mode adds worktree-copy isolation and the bash classifier on top;
  `exec` keeps the sandbox on by default.

Reporting: see [SECURITY.md](https://github.com/cicialgo/rockycode/blob/main/SECURITY.md).

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
`--thinking/--no-thinking`, `--reasoning-effort low|high|max`,
`--max-tokens`, `--context-window` (compaction trigger point), `--max-steps`,
and `--prompt <file>` for system-prompt A/B runs (see `prompts/README.md`).

First runs are slow: the HF dataset downloads once (hundreds of MB), and each
task pulls its official image from Docker Hub (~1 GB each, cached forever
after). On Apple Silicon, enable *"Use Rosetta for x86_64/amd64 emulation"*
in Docker Desktop — the images are x86.

## Architecture

- **Engine** (`rockycode/engine/`) — a model- and UI-agnostic ReAct loop. It
  streams the provider with native tool calling, executes tools, and repeats until the model answers in plain text, emitting a typed event stream — the
  TUI, the bench console, the JSON-RPC server, and the trajectory logger are
  all just subscribers. Chat/serve default to unlimited steps. Finite Engine/bench
  runs default to 100 main calls plus 10 closing calls (`--max-steps` controls
  the main budget). Neutral resource notices leave room for investigation and
  verification, with an honest handoff if the task remains unfinished.
- **Compaction** (`engine/compaction.py`) — before every API call the engine
  projects the next prompt size (the last real `prompt_tokens` plus
  conservative estimates for newer messages). At 50% of the context window a
  one-time nudge appears; at 90% it auto-compacts: first stubbing old tool
  outputs (free and deterministic), then, when more room is needed, one API call folds the older history into a dense state document and the context is
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
  stays serial. Tool outputs are written to teach recovery: errors come back as readable text.
- **Bench runner** — per task: pull the official image → start a container →
  the agent works at `/testbed` → `git add -A && git diff --cached` is the
  prediction → scored by `swebench.harness.run_evaluation`. Agent and scorer
  share the same images.
- **Trajectories** — every session (chat and bench) appends to one global
  store, `~/.rockycode/trajectories/*.jsonl` (`$ROCKYCODE_HOME` moves it):
  metadata (model, prompt name and sha,
  instance id), every message in OpenAI shape, per-call usage (including
  DeepSeek cache hit/miss), and an outcome record. SFT/RL-ready by design.

### Layout

```
rockycode/
├── rockycode/
│   ├── cli/                 # one module per subcommand: chat/exec/config/dream/memory/bench/goal/mcp/serve
│   ├── runtime.py           # composes Engine + MCP/LSP/memory/skills for the TUI and the desktop
│   ├── headless.py          # `exec`: rocky as a subagent for other agents (JSONL contract)
│   ├── server.py            # `serve`: JSON-RPC host for editor clients
│   ├── engine/              # the model- and UI-agnostic core, the lowest layer (tests/smoke_layering.py)
│   │   ├── loop.py          # the ReAct loop (events out, history in, boundary hooks)
│   │   ├── approval.py      # one approval pipeline: policy → grants → loop ticks → verifier → ask the host
│   │   ├── providers.py     # provider registry (DeepSeek, MiniMax, Kimi, GLM, …)
│   │   ├── route.py         # where a model's requests go, and with which key — decided once
│   │   ├── errors.py        # a failed model request as one plain sentence with the next step
│   │   ├── effort.py        # the off/low/high/max dial → provider tiers
│   │   ├── compaction.py    # context compaction (prune → state summary)
│   │   ├── tools.py         # tool schemas + local execution + path jail
│   │   ├── permission.py    # yolo/ask/careful × risk tiers → allow/ask/block
│   │   ├── verifier.py      # opt-in decision models (Jev / Ollama) on the would-ask band
│   │   ├── planmode.py      # the read-only plan-mode gate
│   │   ├── modes.py         # research/learn mode contracts
│   │   ├── mcp.py           # MCP client (stdio servers → extra tools)
│   │   ├── skills.py        # skill discovery + progressive disclosure
│   │   ├── web.py           # web_search / web_research / web_fetch
│   │   ├── container.py     # docker-exec execution + patch extraction
│   │   ├── events.py        # the event contract all UIs subscribe to
│   │   └── trajectory.py    # training-ready session logs
│   ├── goal/                # goal mode: runner, session, branch review, worktree, budget
│   ├── memory/              # files-as-truth memory store + semantic recall
│   ├── dream/               # consolidation, the reflect hook, mining, proposals
│   ├── routines.py          # recurring pre-approved work (leased auto-runs)
│   ├── modes/               # research/learn mode contracts (markdown)
│   ├── skills/              # built-in skills (lean-prover, architecture-viz, artifact-design, rocky-setup)
│   ├── i18n/                # interface text: English and Chinese
│   ├── onboarding.py        # first run and key storage (system keychain, 0600 key file)
│   ├── task_links.py        # linked tasks: handoff and inbox mail between sessions
│   ├── tui/                 # Textual chat app: app.py (the shell), one module per concern, commands.py (the slash table)
│   ├── runners/             # raw baseline · agent-on-SWE-bench · shared data
│   ├── prompts/rocky.py     # built-in system + task prompts
│   └── score.py             # wraps the official swebench eval
├── rockycode-vscode/        # VS Code extension (chat panel over `rockycode serve`)
├── apps/desktop/            # Electron desktop (source preview): Python JSON-RPC host over rockycode.runtime
├── prompts/                 # prompt lab (A/B variants)
├── bench/tasks/dev10.json   # the fast iteration subset
└── tests/                   # docker-free smoke tests (fake model streams)
```

## Prompt lab

System prompts are swappable files. Copy `prompts/rocky-v1.txt`, change one
thing, run dev10 with `--prompt`, and compare score, steps, and tokens. Names
and hashes are recorded everywhere, and each variant keeps its own prediction file. Details in `prompts/README.md`.

## Development

The smoke suite runs offline and keyless, with a scripted fake model stream driving the engine end to end:

```bash
uv run python tests/run_all.py           # the whole gate (what CI runs)
uv run python tests/run_all.py --all     # include the Docker-dependent tests
uv run python tests/smoke_engine.py      # or any single piece by name
```

See [CONTRIBUTING.md](https://github.com/cicialgo/rockycode/blob/main/CONTRIBUTING.md) for conventions and
[CHANGELOG.md](https://github.com/cicialgo/rockycode/blob/main/CHANGELOG.md) for release history.

## About the name

> *"i learn traditional physics. i no know e=mc^2 yet. but we fix bug. amaze!"*

- **rockycode** — Rocky from *Project Hail Mary*: enthusiastic, curious,
  occasionally wrong, gets there anyway. He sings ♪♫ while he thinks.
- **dev10** — the fast 10-task iteration subset; real runs use full Verified.
- **amaze** — what Rocky says when the tests pass.

## License

[MIT](https://github.com/cicialgo/rockycode/blob/main/LICENSE)

## Contribution

Contributions are welcome. The project is still early and has plenty of rough edges, so we like to talk through feature and architecture design with you first. We keep it small on purpose: we picture it as a modified N-1 starfighter, pushed to the limit in a few places, and that's enough.

The initial commit comes from these developers:  
[@cicialgo](https://github.com/cicialgo) — LLM algorithm engineer; overall design and adding the stranger experimental features.  
[@dy2012](https://github.com/dy2012) — LLM engineer and architect; the permission, security, and Docker-based protections, the VS Code extension, coding-capability improvements, and many fixes.  
[@codingmiu](https://github.com/codingmiu) — ML researcher; the standout research-mode designs.
