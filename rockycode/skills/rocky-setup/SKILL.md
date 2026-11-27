---
name: rocky-setup
description: How rocky's own settings work — models, providers, base URLs, keys, limits — so rocky can change its own configuration for a user who asks ("use my proxy", "add my vLLM server", "switch default model", "set my context window"). Load when the user wants rocky itself configured, not their project.
---

# rocky-setup — rocky configures rocky

You have a tool, `rocky_config`, that edits rocky's OWN settings. Use it
instead of hand-editing files with write_file; it validates and knows where
things live. Everything is under `~/.rockycode/`. Keys are the one thing you
never touch (see the last section).

## The pieces

| what | where | how to change |
|---|---|---|
| sticky preferences: `model`, `language`, `currency`, `theme`, `permission`, `image`, `context_window`, `max_tokens`, `vision_models`, `search_max_uses` | `~/.rockycode/config.toml` | `rocky_config set <key> <value>` |
| the model catalog: providers, models, limits, prices, reasoning shapes | shipped `rockycode/models.toml`, user override `~/.rockycode/models.toml` (same shape, deep-merged) | `rocky_config add_provider …`, or the user hand-edits the override |
| an own base URL for a known provider (proxy, gateway, region) | `~/.rockycode/endpoints.toml` → endpoint `<provider>-custom` | `rocky_config set_url provider=… base_url=…` |
| API keys | the OS keychain where there is one, else `~/.rockycode/.env` (0600) | the USER adds them in a hidden field; you name the variable |

`rocky_config show` prints the current state: config values, every provider
with its endpoints and whether each key NAME is present, the known models.
Start there when unsure.

Every change asks the user, each time, in every approval mode: the card shows
the whole change. Three things the tool refuses whoever approves, because the
user must type them:

- a **command** as `image` (a program that would run on their machine): they
  type `/config image <command>` in the TUI or `rockycode config image "<command>"`.
  You can set `auto`, `ask`, `off`, a keyed vision endpoint, or `mmx` by name.
- a **looser** `permission` (ask → yolo, careful → ask): they use the approval
  chip in the status bar, or `/config permission <mode>`. Tightening is fine.
- `add_provider` under a **catalog provider's name** (`kimi`, `qwen-plan`, …):
  that provider's saved key would go to the new address. Use a new name, or
  `set_url` for an own address of a catalog provider.

## Model specs (the `/model` grammar)

`<endpoint>` · `<endpoint>:<model>` · a unique model substring. Endpoints:
`deepseek`, `glm`, `kimi`, `minimax`, `stepfun`, `qwen`, `mimo`, `openrouter`,
`ollama`, plus plan rows `qwen-plan` / `mimo-plan` / `stepfun-plan` (subscription
plans: own URL, own key `ROCKYCODE_<PROVIDER>_PLAN_API_KEY`) and own-URL
rows `<provider>-custom`. Examples: `deepseek-flash`, `glm:flash`,
`qwen-plan:qwen3.8-max`, `openrouter-free`, `ollama:qwen3.8:27b-mlx`.
OpenRouter is free and not the launch default. Its key is
`ROCKYCODE_OPENROUTER_API_KEY` (not the verifier's `OPENROUTER_API_KEY`).

- `/model <spec>` switches THIS session (host command; you cannot run it).
- `rocky_config set model <spec>` sets the launch default.

## Limits

`context_window` and `max_tokens` default to `0` = follow the active model's
registry entry (they change with `/model`). A positive number pins the
user's own ceiling across switches. Effort dial: `off | low | high | max`.

## Recipes

- "use my proxy for deepseek": `rocky_config set_url provider=deepseek base_url=https://my-proxy/v1` → then the user runs `/model deepseek-custom`.
- "add my vLLM server at http://box:8000/v1 serving my-model-7b": `rocky_config add_provider provider=box base_url=http://box:8000/v1 models=my-model-7b reasoning=none` → key name `ROCKYCODE_BOX_API_KEY` (a keyless server still needs any non-empty value there, or `local = true` in `~/.rockycode/providers.toml`).
- "images don't work on pro": explain `image = auto` describes via deepseek-flash on the same key; `rocky_config set image mmx` names a local vision CLI rocky knows, `set image kimi:kimi-k3` pins an endpoint; or `rocky_config set model deepseek-flash`. Any other image command is the user's to type (`/config image <command>`).
- "cost shows 0 for kimi": its price isn't in the registry — the user adds `[models."kimi-k3".price]` to `~/.rockycode/models.toml` (usd/cny tables from Moonshot's price page), or `~/.rockycode/pricing.toml`.
- "try rocky without a DeepSeek key": the user sets `ROCKYCODE_OPENROUTER_API_KEY` (from https://openrouter.ai/keys) and runs `/model openrouter-free`. Free tier is about 50 requests/day and 20/minute until the account has bought at least $10 of credits (then 1,000/day). Do not set this as the launch default unless they ask.

## Keys — never through you

Never ask for, accept, echo, or write an API key. If a user pastes one,
say you can't handle it and point them to the hidden field: `/model` → "add
or change a key" in the TUI, or the key form in the desktop app (the runtime
"?"). It goes to the OS keychain where there is one. The key's name is
`ROCKYCODE_<PROVIDER>_API_KEY` (DeepSeek's is `ROCKYCODE_API_KEY`).
`rocky_config` refuses key-shaped values by design. rocky reads only these
rocky-owned names — never an ambient `OPENAI_API_KEY` / `MINIMAX_API_KEY`.
Do not read or print rocky's key file or settings files from the shell: a
command that names them, or `~/.rockycode` as a whole, needs the user every time.
