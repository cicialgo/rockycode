# Changelog

All notable changes to rockycode are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/) — pre-1.0, so the surface may still
change between minor versions.

## [Unreleased]

## [0.2.0] — models are data, loops, approval switch

### Added
- **Loops** (`/loop`, alias `/cron`): re-fire one prompt into THIS chat on an
  interval — `/loop 5m check whether results/run3 has a summary.json; if so
  give me the headline numbers`. Each tick is an ordinary turn on the session
  engine (it sees the whole conversation), runs only when the chat is idle,
  ends with `LOOP QUIET / NOTE / DONE`, and never prompts: a call that needs
  approval pauses the loop (`/loop allow` decides it, `/loop resume` retries,
  `/permission yolo` or shift+tab resumes every approval-paused loop; a manual
  `/loop pause` stays paused). Quiet ticks fold into one streak line and roll
  out of live context (the trajectory keeps them). No cap unless you set one
  (`for 3h`, `x12`, `max $2`); soft running-cost reminders otherwise. Rocky
  can start one itself (`loop_start` / `loop_stop`, through the normal
  approval). Session-only — a loop dies with the session; `/routines` stays
  the cross-launch scheduler.
- **Models are data.** The whole model catalog now lives in
  `rockycode/models.toml` — per provider a China base URL, a rocky-owned key
  name, a reasoning wire shape (`thinking` · `effort` · `enable_thinking` ·
  `minimax` · `openai` · `none`), its own effort tiers, whether thinking can
  be switched off, and where usage reports cache hits; per model its context
  window, output cap, vision flag, price tables (USD + CNY) and roles. The
  engine consumes a `ModelSpec` and carries no model-specific numbers of its
  own. `~/.rockycode/models.toml` (same shape) is deep-merged on top: add a
  model, correct a limit, add a price, `hidden = true` to drop a row.
- The 2026-09 roster: `deepseek-flash` (V4.1 Flash — native vision, the
  default and the sidecar/search model) + `deepseek-v4-pro`; `glm-5.3` +
  `glm-5.3-flash`; `kimi-k3`; `minimax-m3`; `step-5-preview`; `qwen3.8-max`
  + `qwen3.8-flash`; `mimo-v2.6-pro`; `ollama` (live-discovered). Limits and
  DeepSeek's V4.1 prices verified at each provider's docs on 2026-09-29.
- Subscription-plan endpoints as their own picker rows: `qwen-plan` (Bailian
  Token Plan), `mimo-plan`, `stepfun-plan` — own URL, own key
  (`ROCKYCODE_<PROVIDER>_PLAN_API_KEY`).
- Context window and output cap follow the active model: config
  `context_window` / `max_tokens` default to `0` (= the registry value) and
  re-pace on every `/model` switch; a positive number pins your own ceiling.
  `/config context_window 0` un-pins.
- `/effort off|low|high|max` — the dial now reaches `low` (DeepSeek, GLM and
  Kimi all take low|high|max), is clamped onto each provider's own tiers by
  position at the wire (StepFun's `low|medium|high` gets `medium` for
  rocky's `high`), and sends the lowest tier for `off` on a model that
  can't disable thinking (GLM, Kimi). The status line says what is sent.
  `xhigh` stays accepted as `max`.
- `image_route auto` (new default): a pasted image on a text-only model is
  described silently by the registry's sidecar (`deepseek-flash` on the home
  key) or your image CLI — no picker in the way; `ask` keeps the picker.
- **Rocky configures rocky.** A built-in `rocky-setup` skill plus the
  ask-tier `rocky_config` tool (show · set · set_url · add_provider) let a
  user ask rocky to use a proxy, add a vLLM box, change the default model or
  a limit — writes go under `~/.rockycode` only, through the same validated
  setters as the CLI. Key-shaped values are refused with the env var to use.
- `rockycode exec --profile read|write|full`: `read` (read_file/grep/glob/
  view_image) and `write` (+ jailed write_file/edit_file) have no shell, so
  they run on the host with no Docker and start instantly — the profiles a
  calling agent (Claude Code, Codex) wants for "look and tell" and small
  edits. `full` keeps bash in the Docker sandbox by default. The stream is
  now meta → text → result by default; `--events` restores the tool.*/turn.*
  receipt lines (`--include-thinking` implies it). `meta.profile` reports
  name, mode and tool set.
- Peak-hour pricing honors DeepSeek's calendar: Monday–Friday only, with a
  `holidays` list (ISO dates, Beijing) on the schedule for Chinese public
  holidays — weekends and holidays bill off-peak.
- Approval mode is switchable without typing: `shift+tab` steps it one notch
  looser (careful → ask → yolo, wrapping back to careful, so a run of presses
  never parks you in yolo). The status-bar 🔒 chip now spells that key out and
  opens a picker when clicked — three rows saying what each mode actually
  allows — and bare `/permission` opens the same picker. The cycle stays inert
  while an approval prompt is waiting, and landing in yolo still prints the
  "runs on your machine" warning.
- Local models: a builtin `ollama` provider (`http://localhost:11434/v1`,
  override with `ROCKYCODE_OLLAMA_URL`) — keyless, priced `$0 · local`, model
  list discovered live from the running server's `GET /v1/models` so the
  `/model` picker shows what's actually pulled. Server down → a dimmed
  "not running · start it: ollama serve" row instead of silence.
- `/model` readiness preflight for local providers: before the engine is
  switched, rocky checks server reachability, that the model is pulled, that
  it supports tool calling, and the serving context length (Ollama truncates
  silently — the #1 agent-loop killer). Not ready → the switch is refused and
  every failed check carries its exact fix (`ollama pull …`,
  `export OLLAMA_CONTEXT_LENGTH=65536`); ready → rocky's `context_window` is
  paced to the server's verified value (restored on switching back to a cloud
  provider), and a server-reported vision capability turns image input on.
- Keyless endpoints: `local = true` on a `~/.rockycode/providers.toml`
  provider (LM Studio, llama.cpp, vLLM, a remote box's ollama) marks its
  endpoints keyless — always configured, no placeholder-key hack needed.
- Compaction on a local provider sends no tool schemas in the summarize call
  (local compat layers ignore `tool_choice="none"` and may answer with a tool
  call instead of a summary), and the thinking-off field is now shaped per
  the provider's reasoning policy instead of always DeepSeek's.
- Vision is now per-MODEL, not per-provider (`Provider.vision_models`,
  choice-level `❖` badge). Config key `vision_models` marks additional ids as
  image-capable from any shell (`rockycode config vision_models <id>`) — for
  when a provider ships vision on an existing model before the registry
  catches up.
- Config key `model`: a sticky launch default (`rockycode config model
  <spec>`), resolved against the registry; precedence `--model` flag →
  `ROCKYCODE_MODEL` env → config. Global config only — a cloned repo can
  never redirect requests.
- The `/model` picker is now two-step, model first: one row per model, and a
  model with several endpoints then asks which URL serves it — including a
  "custom base URL" row that remembers your own gateway/proxy per provider
  (`~/.rockycode/endpoints.toml`, addressable as `<provider>-custom`, riding
  the provider's key).
- Images are best-effort downscaled to 2048px before hitting the wire (PIL if
  installed, macOS `sips` otherwise, original on any failure) — vision
  providers bill image tokens by dimensions, and a Retina screenshot was
  paying severalfold for nothing.

### Changed
- One China endpoint per provider (no more `kimi-cn`/`kimi-en`, `zai`/`glm-cn`,
  `minimax-cn`/`-en`): `/model kimi`, `/model glm`, `/model minimax`. Keys are
  `ROCKYCODE_<PROVIDER>_API_KEY`; the older `_CN_`/`_EN_`/`ZAI_EN` names are
  still read as aliases (the picker names the alias in use), so no setup
  breaks. An international or proxy URL is the custom-URL row.
- `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are retired from
  the list (DeepSeek retired the models on 2026-09-10 and serves those names
  with V4.1-Flash); rocky aliases both to `deepseek-flash`, so old configs,
  trajectories and `/model` habits keep working. `deepseek-v4-pro` stays
  listed, labelled: since 2026-09-14 DeepSeek routes it to V4.1-Flash at
  Flash rates until V4.1-Pro ships.
- `glm-5.2` and `step-3.7-flash` drop off the roster in favor of GLM-5.3 /
  Step 5 Preview.
- Session titles, image describe, and the native web search all use the
  registry's `sidecar` / `search` role (deepseek-flash) — a session on Kimi
  or GLM no longer sends a DeepSeek model id down its own client for the
  title call.
- `--max-tokens` / `--context-window` default to `0` (= the model's registry
  value) on `chat` and `serve`; `bench` keeps its pinned reproducible numbers.
- The CLI help, status line and context reminder no longer speak of
  "DeepSeek V4" as the only model.
- `view_image` on a vision-capable active model now attaches the real image
  to the conversation (as the next user message) instead of a sidecar text
  description — the model reads the pixels and decides what matters itself.
  Text-only models keep the describe routes unchanged.
- Launch honors the registry: starting with `--model minimax-m3` (or any
  registry model) now gets the right reasoning params, tools flag, and vision
  capability from step one instead of DeepSeek-shaped defaults until the
  first `/model` switch. Unresolved specs and injected clients behave exactly
  as before.
- `/model` spec resolution prefers exact ids, and a base model wins its own
  substring — `glm:5.3` means `glm-5.3`, not ambiguity with `glm-5.3-flash`;
  the variant stays reachable via `glm:flash`.

### Fixed
- The prompt-cache observer and the cost ledger read cache hits from
  OpenAI-style `prompt_tokens_details.cached_tokens` too (Kimi, GLM, MiniMax,
  Qwen, MiMo), not only DeepSeek's `prompt_cache_hit_tokens`.
- Launching on a `<provider>-custom` endpoint of the home provider now uses
  that URL (it used to fall back to the env base URL).

## [0.1.2] — `--version` flag, GA price tables

### Added
- `rockycode --version` / `-V` prints the installed version. One version
  source: package metadata (pyproject) — the serve handshake reports the same
  value instead of a hardcoded string.

### Changed
- DeepSeek price tables refreshed to the GA snapshots (V4-Flash-0731 /
  V4-Pro-0813), both USD and CNY, verified 2026-08-20 at the source;
  peak-valley billing confirmed live (2× in the published UTC windows).
- README results updated: `deepseek-v4-flash` GA carries three clean full-500
  SWE-bench Verified rounds (88.8% average, 95.4% pass@3); the
  `deepseek-v4-pro` column is explicitly marked preview (pre-0813).

### CI
- Release workflow reduced to the single ubuntu PyPI Trusted-Publishing job;
  the dead macos-13 matrix (retired runner, never ran) is gone.

## [0.1.1] — session artifacts, images in chat, real sandbox cancel

### Added
- Images in chat: paste (`ctrl+v` / `/paste`) or drag an image in. Vision models
  see it raw; no-vision models route through a provider sidecar, your own image
  CLI, or a `view_image` tool — picked once, remembered. Known CLIs set up with
  one word (`/config image_cli mmx` — rocky knows the invocation).
- Session artifact inventory: `/artifact list · open <n> · stop · live on|off`,
  a footer badge with open-tab counts, and an Artifacts tree in the VS Code
  extension fed live by `rockycode serve`.
- Bare `/model` opens a live provider + model picker.

### Fixed
- Live artifacts no longer drop and reconnect every 30 s; the artifact server
  stops/restarts cleanly and rebinds saved live pages to the new port.
- Sandbox cancel/timeout kills the in-container process group, not just the
  host-side docker client; images without `python3` fall back to plain `bash -c`.
- Resume self-heals after a hard kill; closing the doc dock no longer wedges the TUI.

### Internal
- CI: conservative ruff correctness gate (pinned `0.16.1`) ahead of the smoke suite.

## [0.1.0] — first public release

Initial public release. One repo, one engine, three ways to use it — interactive
`chat`, autonomous `goal`, and the `bench` measurement rig — running on DeepSeek
or any OpenAI-compatible model.

Some capabilities ship as **experimental and default-off** (self-improvement,
`prove` / `lean-prover`, `explore`, and providers other than DeepSeek); see the
README's Experimental section for what they are and how to enable them.
