# Changelog

All notable changes to rockycode are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/): from 1.0.0 on, a breaking change to the
CLI, `serve` or `exec` contracts bumps the major version.

## [Unreleased]

## [1.0.0] — one engine, one approval pipeline, more models, a desktop preview

The core is now a layered engine behind one approval pipeline shared by the TUI,
`serve` and `exec`; UI text is one TOML file per language for the terminal and
the desktop; DeepSeek Flash 1017, MiniMax M3.1 Flash and OpenRouter's free router
are first-class; the Electron desktop ships in `apps/desktop` as a developer
preview; and self-improvement (evolve) is opt-in from this release on.

### Added
- **Desktop Model Race: a finish board.** Starting a race now brings the lanes into view, with
  a board above them where five flags wait at the start. When the race is over, it runs again
  on that board in about two seconds: each lane's flag reaches the finish line in the order
  the lanes really finished, with its place and its time. A lane that failed or was cancelled stops
  short and gets no place, however fast it failed. The order is by time only, and the board
  says so; the lanes stay anonymous (A–E) until you save your review or reveal the names,
  and then the names come in on the board too. "Replay" runs it again. No motion when the
  system asks for reduced motion. The offline fixture's lanes now finish at different times,
  so an offline race has an order to show.
- **Keys in the OS keychain — any key, not only the home key.** Where the machine has a
  keychain (macOS Keychain, Windows Credential Manager, a Linux Secret Service), a key saved
  from the first run, the TUI's key field or the desktop's key form goes there first. Each key
  is its own entry (service `rockycode`, the variable's name), and the key file then lists only
  its name (`ROCKYCODE_KEYCHAIN=…`), never its value. The 0600 key file stays as the other
  choice in each of those places, and as the only one where there is no keychain. A start
  reads back exactly the listed names; a shell export still wins; a store with nothing listed
  never opens the keychain. `/model` → "add or change a key" says where each key lives and has
  a row that moves the keys still in the key file, each read back from the keychain before its
  line leaves the file. A keychain that gives nothing (locked, an ssh or scheduled session) is
  said so instead of reading as a key never added, and one that will not take a key saves
  nothing: rocky says so and asks again, and the key file is used only when you pick it. A
  relocated store (`$ROCKYCODE_HOME`) keeps its keys in its own file. The keychain trusts the program that saved the key, which
  here is the Python that runs rocky: it keeps a key out of files, backups and other tools'
  reach, not away from code run by that same Python.
- **Add a key inside the desktop app.** The first-launch card now has a form: pick where your
  model comes from, paste the key (a hidden field), and the app is ready without a relaunch.
  The same form sits in the runtime dialog ("Add or replace a provider key") for later keys.
  The host tests the key with one free request and saves it with the core's saver (the system
  keychain where there is one and the box stays ticked, else the 0600 key file with every
  other line kept); a rejected key is said in place and never written. The key is
  not kept in the page, the app's data folder or any task. The terminal command stays on the
  card as the other way in.
- **Add a key inside the TUI.** Picking a model whose provider has no key — in the `/model`
  picker or by name — opens a hidden field instead of printing an env-file recipe that meant
  quit, edit, restart. rocky tests the key with one free call, saves it (the OS keychain where
  there is one, or the 0600 key file with every other line kept — the field's rows say which),
  applies it to the running session and makes the switch. To replace a key, or add one ahead of time, the
  `/model` picker has an "add or change a key" row: pick the endpoint and the same field opens.
  No command takes a key, so there is nothing to type one next to. The key never enters the
  chat input, its history, the transcript, the session record or a model request.
- **Artifacts you can come back to.** An HTML artifact now has a stable ID and numbered
  versions, kept in the project's `.rockycode/artifacts/` across restarts. A new version never
  overwrites an old one, and an edit made from a stale version is refused instead of silently
  replacing newer work. In the TUI a saved artifact is a card in the chat with **Open**,
  **Edit**, **History** and **Copy path**; `/artifact` opens a searchable picker for the whole
  project (one click narrows it to this session). **Edit** ties your next message to that
  artifact (your draft stays; a ✕ chip in the status bar, or `/artifact cancel`, lets go of
  it), and **History** opens any older version or restores it as a new one. The model reads
  an artifact with the new `inspect_artifact` tool before it revises one. A loose HTML file
  from before is never claimed on its own: `/artifact import <file>` takes it in as a new
  artifact and leaves the original alone.
- **A page for artifacts.** When rocky writes an artifact as plain HTML (a heading,
  paragraphs, a table, a drawing), rocky's own page now gives it the type, the spacing and the
  colours, in light and in dark (dark follows your system). The model chooses no colours and
  no fonts, so an artifact no longer depends on a model's taste that day: the first paragraph
  is set as the answer, a `<blockquote>` is a note, and a drawing takes the page's colours
  through four classes instead of hard-coded ones that vanish in the dark. Three looks:
  **ink** (the default), **amber**, and **lilac** (rocky's purple). Pick yours with `/config
  artifact_theme lilac`; `none` keeps the type and drops the colours. A saved artifact keeps
  its look when you change the default. System fonts only, nothing loaded from the network,
  and every text colour is at least 4.5:1 on its background. A new built-in skill,
  `artifact-design`, tells the model how to write for the page. A full HTML document is
  still kept exactly as written, for when you want your own design.
- **Call identity, phase 1** (#99): every tool call now carries an envelope — session, turn,
  model request, physical attempt, a host-minted logical call id beside the provider's call
  id, the catalog generation the request was built against and the resolved route (never a
  key). It rides on `ToolStarted` / `ToolFinished`, on `serve` notifications and `exec` JSONL
  lines as `envelope` (turn events carry `turn`), and the trajectory gets one `request` line
  per model request (SDK retries counted on the real transport) and one `call` receipt per
  call: `ok`, `error`, `refused`, `unknown` (cancelled after dispatch) or `cancelled`.
  `Runtime.publish_registry` is where MCP and LSP tools join the catalog and advances the
  generation; an explore child records its parent turn, request and call. Nothing changes
  behaviour; binding a request to a catalog snapshot is phase 2.
- **Kimi K3.1 Pro** (`kimi-k3.1-pro`) on the Kimi provider: vision, 1M context, thinking
  always on (the effort dial maps onto its low | high | max). Requests send
  `max_completion_tokens` and no sampling parameters, and the assistant's reasoning is
  replayed on tool follow-ups and later turns, as the model's contract asks. Covered by the
  offline protocol tests; not yet called against the live endpoint.
- **Qwen4 Max Preview** (`qwen-plan:qwen4-max-preview`) on the Bailian Token Plan, and only
  there: the model is not sold pay-as-you-go, so the ordinary `qwen` row does not offer it
  and it never falls back to another key. Vision, 1M context, a 393,216-token output budget
  shared with thinking. The effort dial maps onto its low | medium | xhigh, off turns
  thinking off, and the assistant's reasoning is replayed on tool follow-ups and later
  turns. Called against the live endpoint on 2026-11-12. Alibaba runs this preview until
  2026-11-25 24:00 (UTC+8) and takes it offline afterwards.
- **Desktop theme: Nebula.** The sidebar has a theme picker with two buttons. *System*
  follows the OS light or dark setting, as before. *Nebula* is the terminal's own palette
  on the desktop: night surfaces, lavender text, violet for what matters, amber for
  warnings, red for errors, and no green. One click switches; the choice is saved, and the
  app opens in it from the first frame. Nebula covers every view, the Model Race view
  included, and a test fails when a colour is added without a Nebula value.
- **Note strip.** Once a project has two tasks, the terminal shows them under the title as
  one row of notes: `♪ 01 fix login   ♫ 02 write tests ✉1   ♩ 03 docs`. Click a note to
  switch to that task, or type its number: `/task open 2` (the tooltip says so); the
  current one is highlighted and `✉n` counts the messages waiting in a task's inbox. A
  narrow terminal keeps the current task and folds the older ones into `+N`. An ordinary
  chat never shows the strip, and a note never claims another task is running: one
  terminal runs one session. An untitled task takes its session's name.
- **Desktop developer preview** (`apps/desktop`, README in English and 中文). The desktop is in
  the public tree with its manual CI workflow and the model-race smoke; the internal planning
  and audit notes stay private. An Electron + React host over the shared Runtime, run from a
  clone (`uv sync`, then the npm steps). Each
  task keeps its own model (never the CLI default); approvals are explained in plain words
  with the tool, its arguments and what a grant covers; starting an MCP or LSP process asks
  first, showing server, config source and command, and "remember" binds to this workspace
  and exact config until exit; HTML artifacts preview in a sandboxed in-app pane with an
  explicit open-in-browser action; the UI is en/zh from the shared locale files; a five-model
  race (田忌赛马) scores the same task with human grades and an anonymous judge. Dependencies
  are pinned and reviewed (Electron 44.9.1, Vite 8.3.5, npm install scripts off, explicit
  checksum-verified Electron download). No installer, signing or auto-update yet.
- **Desktop update reminder.** The desktop can GET a small version file
  (`https://rockycode.ai/desktop/latest.json`) and show a banner with a link when a newer
  version exists; "Later" hides that version. The check at launch (at most once a day) is off
  until you switch it on in the sidebar; "Check now" and the app menu's "Check for Updates…"
  work either way. Nothing downloads or installs by itself and nothing about the user is sent.
- **Plan mode** (`/plan`): the session becomes read-only except for one plan the model
  drafts and revises with you (revision-bound approval, `submit_plan`); approve to build here
  or hand it to `goal`. `serve` exposes the same flow as `plan/*` RPCs.
- **Shared Runtime** (`rockycode.runtime.build_runtime`): one composition of the Engine with
  MCP, LSP, memory, skills and model clients and their lifecycle, used by the CLI, `serve`
  and the desktop host.
- **Finite chat budgets.** A turn runs on a step budget (100 steps, then a 10-step close-out
  with neutral resource notices rather than pressure) and resumes at a clean request
  boundary without resetting it.
- **`exec --resume <session> --allow <grant>`** carries a headless session's history into a
  new one with an explicit follow-up task; grants apply to this invocation only, nothing is
  replayed, and tool receipts are recorded before the finished event so an interrupted run
  stays inspectable.
- **Queued and interruptible delivery.** A message sent while rocky is working interrupts
  by default or queues after the turn (`/queue on|off`); `/queue` inspects,
  resumes or clears pending messages. The desktop host shares the same delivery.
- **MCP: native model selection for Dream, Goal and routines.** `rocky_dream`, `rocky_goal`
  and `rocky_routine_run` accept a registered model id (and `reviewer_model` for Goal);
  the request binds to that endpoint's own key, wire id, effort shape and protocol, and a
  receipt digest is part of every preview and re-checked before a queued job runs. Dream
  over MCP previews without a model call by default and runs only with the digest plus an
  explicit `share_project_context=true`.
- **Decision providers.** One task-independent `DecisionClient` behind the tool verifier
  for TypeSafe Jev, OpenRouter and local Ollama. `rockycode/decision_eval.py` replays 73
  labelled multi-tool cases (`examples/verifier-cases.jsonl`) through the real approval
  gate and reports effective-approval metrics.
- **OpenRouter free models** so rocky can be tried without a paid DeepSeek (or
  other) key. Provider `openrouter` speaks OpenAI chat completions at
  `https://openrouter.ai/api/v1`. Key is `ROCKYCODE_OPENROUTER_API_KEY`
  (create one at https://openrouter.ai/keys). The verifier's
  `OPENROUTER_API_KEY` is a different variable and does not reveal these rows.
  DeepSeek stays the default: neither model has a `default` role, and the
  picker lists them only when this key is set. `/model openrouter` (or
  `/model openrouter-free`) sends `openrouter/free`, the Free Models Router,
  which picks a free model that supports the request, including tools.
  `/model openrouter-laguna-s` pins Poolside Laguna S 2.1's free variant
  (`poolside/laguna-s-2.1:free`), a coding-agent model, because the router
  can land on a very small one. Reasoning fields are not sent
  (`reasoning = none`); there is no audited `protocol`. Prices are omitted
  ($0; the footer stays "prices unset"). The router's rocky context/output
  clamp is 65,536 / 8,192 — the smallest tool-capable `:free` window on
  2026-10-23 — not OpenRouter's published 200,000 ceiling. Laguna S is
  262,144 / 32,768. Free-tier limits (OpenRouter docs, same day): 20
  requests/minute and 50/day until the account has bought at least $10 of
  credits; 1,000/day after that. Some free providers log prompts.
  Groq and Gemini's OpenAI-compatible free endpoints are not builtin rows;
  point `~/.rockycode/providers.toml` or `endpoints.toml` at them.
- Design comparison of MaD-RSI (Cui et al., arXiv:2610.28201) with rocky's harness and self-improvement path, in `docs/design/mad-rsi-harness-comparison.md`.
- Lean proofs can be graded with `lake check` when the toolchain ships it
  (Lean v4.35+), and with `lake check --paranoid` on request. A sorry-free
  compile is **green · compiled**. A sandboxed kernel recheck of a file on a
  default target is **green · kernel-checked**. Every bundled kernel accepting
  it is **green · paranoid**. If the compiler accepts a file and a kernel
  rejects it, the verdict is **disputed**: the file and the check log stay,
  and the repair loop does not continue. Older toolchains and hosts without
  the Linux bubblewrap sandbox stay at green · compiled and say the recheck
  did not run. `--inadvisably-no-sandbox` is never the default.
- **MCP capability bridge** for Codex, Claude Code, DeepSeek Harness and URL-based
  local clients: explicit exec continuation/context, background jobs, project
  memory/skills, decisions/verifier advice, Dream/proposal review, reviewed Goal
  plans and one-shot routines. Authenticated loopback Streamable HTTP is optional;
  stdio remains the default. See `docs/mcp-server.md` for the capability matrix,
  client setup, consent contracts and remaining gaps. No dependency changes.
- **MiniMax-M3.1-Flash** as `/model minimax-m3.1-flash` (the request sends
  `MiniMax-M3.1-Flash`). Same MiniMax key as M3 (`ROCKYCODE_MINIMAX_API_KEY`;
  the `_CN_` / `_EN_` aliases still work). China OpenAI base URL
  `https://api.minimax.cn/v1`; international is `https://api.minimax.io/v1`
  via `~/.rockycode/endpoints.toml`. 1,000,000 context, 131,072 output cap
  (API maximum 524,288), image and video input, and published prices
  (USD $0.15 / $0.60 per 1M input / output, cache read $0.015; CNY ¥1 / ¥4,
  cache read ¥0.10). Thinking cannot be turned off — `/effort off` sends
  `reasoning_effort=low` instead of `thinking.type=disabled`, which this model
  rejects with HTTP 400. See `docs/minimax-m3.1-flash.md`.
- **MCP server for Codex** (`rockycode mcp`). A local stdio Model Context
  Protocol server — distinct from `rockycode serve`, which stays rocky's own
  JSON-RPC for editor clients. Exposes `rocky_exec`, which runs the same
  headless path as `rockycode exec` and returns the summary, evidence (and a
  temp output file when there is an answer), token usage and USD cost when
  the model is priced, and the exec exit status. Default profile is `read`;
  write and full run only when the caller passes them. API keys are scrubbed
  from the tool result; protocol bytes stay on stdout and logs on stderr.
  See `docs/mcp-server.md`. Optional Codex plugin skeleton:
  `integrations/codex-plugin/` and `.agents/plugins/marketplace.json`.
- **Perplexity and local MLX decision providers** for the tool verifier (off
  unless auto-mode is already on). `provider=perplexity` posts the same Choice
  question to `https://api.perplexity.ai/v1/decisions` with model
  `pplx-decider-v1.1-27b` (`PERPLEXITY_API_KEY`; `ROCKYCODE_VERIFIER_API_KEY`
  or `ROCKYCODE_JEV_API_KEY` still win when set). `provider=systemone` calls a
  local MLX `/v1/systemone` server (default `http://127.0.0.1:8092/v1/systemone`)
  and does not require a cloud key. These are decision models, not chat models,
  and they are not added to `models.toml`. See
  `docs/design/pplx-decider-decision-provider.md`.
- **Jev auto-mode** (chat permission gate, off by default). After the static
  block/allow decision, an opt-in harness client can ask TypeSafe Jev
  (`jev-1.13.0`, or the same Choice call via OpenRouter) whether a would-ask
  tool call should be allowed, asked, or denied. `shadow` only logs; `enforce`
  can auto-allow a high-confidence `allow`. Timeouts, HTTP 429, malformed
  answers, and a missing key fail closed (`ask` by default, or `deny`) — never
  a silent allow. Yolo does not call Jev. Block-tier commands stay blocked.
  Jev is not a chat model and not a tool. See `docs/jev-auto-mode.md`.
- **Catch-up run state for unattended briefs.** `rockycode/catchup.py` keeps
  per-source bookmarks, a reported-items ledger and posting/posted/maybe-posted
  run records; ledger and bookmarks move only after a confirmed post, and a
  source that failed to read keeps its bookmark. Template routine in
  `docs/templates/catchup-brief/`; comparison with Anthropic's agent-automation
  guide in `docs/design/offline-agent-guide-comparison.md`.

- **Desktop prompt presets.** The desktop composer can save, rename, delete and apply named
  prompt templates. Presets are global across tasks and stored in the desktop user-data
  directory (`prompt-presets.json`, mode 0600), not in the project and not beside model
  credentials. Applying a preset replaces the task input, and asks first when that draft is
  not empty. A preset is only a name and prompt text.
- **MiniMax server-side web search, and a search budget.** `ROCKYCODE_SEARCH_MODEL` picks the
  native search provider independently of the chat model (`minimax-custom:minimax-m3.1-flash`
  reaches MiniMax's Anthropic-compatible `web_search_20250305`; DeepSeek stays the default).
  The loop follows `pause_turn` for up to six requests per turn under a 240 s deadline, and
  the per-request server-search cap is the new `search_max_uses` setting (default 12, 0 = no
  cap; read from the global config only, because providers bill each search — MiniMax
  $0.01). Until now DeepSeek search carried a fixed cap of five. Live-checked on the
  international MiniMax host; the ledger still counts tokens only, not search fees.
- Linked tasks (#95): a session can create another task with an explicit handoff and leave
  it messages. TUI: `/task new <handoff>` · `open <id>` · `list` · `send <id> <message>` ·
  `inbox` · `use <message-id>` · `dismiss <message-id>`; the desktop gets a "Create task /
  messages" panel on each conversation. Four model tools — `create_new_task` and
  `send_task_message` go through the risky-tool approval, `read_task_messages` and
  `list_tasks` are safe reads. Creation is draft creation: nothing is copied from the parent
  and the user sends the draft; mail is stored reference data that never starts a turn or
  grants anything. Tasks live in `task-links.sqlite3` (TUI: the rocky home; desktop: its
  data directory), so the two surfaces do not see each other's tasks. Review fixes folded in
  before merge: refusals are catalog-keyed on both surfaces, a TUI switch runs the ordinary
  exit flow (outcome record, final reflect) and keeps the terminal's permission mode, a
  session becomes a task only on first `/task` or tool use, listings stop at the 50 most
  recent tasks, and rows whose session file is gone are pruned.

### Changed
- **The welcome line and the tagline say "we build together"**, not "we fix bug": rocky builds,
  explains and researches too. The default prompt already said so.
- **An artifact keeps the look it was written with.** rocky used to strip a page's own
  styles and wrap everything in its light-purple page. A full HTML document is now saved as
  written (plus a charset line when it has none, so Chinese text survives being opened as a
  file). A fragment gets rocky's own page in your look (see "A page for artifacts" above);
  purple is one of the looks, not the default.
- **The TUI no longer opens your browser when an artifact is saved.** It shows the card and
  you open it when you want; the model is told the page was saved, not that it was opened.
  The old "live or once?" question is gone: live preview is `--live`, `/artifact live on`, or
  the picker's **Live** button. Other hosts are unchanged: `exec` and the editor extension
  still present each saved page, and the desktop shows it in its panel.
- **The artifact count in the TUI's status bar, `serve`'s artifact list and a desktop task's
  list are this session's artifacts.** The project's full inventory is the `/artifact` picker.
- **Qwen4 Max replaces the preview** on the existing Bailian Token Plan route.
  Select `qwen-plan:qwen4-max`; saved `qwen4-max-preview` IDs resolve to the stable
  model with the same endpoint and key. The effort tiers are low | high | xhigh.
  The shared output cap is now 131,072 tokens, valid with thinking on or off
  (the API permits 262,144 for thinking plus the answer in thinking mode).
  Offline request-contract regressions cover this migration; the November 12
  live preview result above does not validate the new stable model.
- **`keyring` is installed by default on macOS and Windows**, where the system always has a
  keychain (about 670 KB with its four pure-Python dependencies), so a new key can go there
  without an extra to remember. Linux keeps the opt-in `rockycode[keyring]` extra: it needs a
  desktop Secret Service and pulls `cryptography`.
- **A failed model request is one plain sentence with the next step.** The error line was the
  SDK's text: `AuthenticationError: Error code: 401 - {'error': {…}}`. rocky now says what
  happened and what to do — a rejected key (with the key's source, and where to paste a new
  one), an account out of credit, a rate limit, a host that cannot be reached or did not
  answer, a conversation too long for the model, a model the provider does not know, a
  refused request, a provider-side problem — and keeps the SDK's own text as a dim second
  line. The TUI words it in the session's language; the desktop shows its own translated
  sentence instead of the raw text. `exec`'s JSONL error event and `serve`'s `session/error`
  gain a `kind` field (`auth`, `credit`, `rate_limit`, `timeout`, `network`, `context`,
  `model`, `request`, `server`); `message` is now the sentence, and `serve` also sends the raw
  text as `detail`. The key's source is no longer appended to a context-length error.
- **The transcript says what rocky did.** A tool call is one line that names what it acts on
  — the command, the file, the query (`✓ read_file app.py`, `✓ bash python -m pytest -q`) — and
  the line a call starts with becomes its result line. It was two lines per call, and neither
  said which file or command; in auto-approve mode nothing on screen did. The desktop's tool
  rows show the same summary, and a failed call has its own icon instead of a check mark.
  Secret-shaped text is masked and the content being written is never shown. A resumed
  session replays its calls with their targets too.
- The hint bar under the input is short enough for an 80-column terminal: `drag text = copy ·
  /help · /model · /plan · /goal · /exit · type / for more`. The long list was cut off before
  `/clear` and `/exit` on most terminals and had no `/plan` or `/goal`.
- **A new model is one row.** A model's special request rules used to be a named contract
  in rocky's code (`protocol = "…"` in the row pointed at it), so a model with a new mix of
  rules needed a code change. The rules are now switches in the model's own row in
  `models.toml`: `replay_reasoning` (`never`, `with_tools`, `always`), `reasoning_required`,
  `strict_finish`, `tool_choice_none`, `output_limit` (`max_tokens` or
  `max_completion_tokens`) and a `sampling` table. A row in `~/.rockycode/models.toml` can set
  them for a model rocky has never heard of, or change one on a builtin row. The five builtin
  models that had a contract send the same requests as before. `protocol` is no longer a row
  field. A new thinking wire format, or a behaviour no model has had yet, still needs code.
- A model row rocky cannot read is refused out loud instead of skipped in silence: an
  unknown field (with a "did you mean"), a switch with a value it does not take, a number
  that is not a number. Asking for that model — at launch, in `exec`, with `/model`, from the
  desktop or MCP — names the row, the reason and the file to fix, and the `/model` picker
  keeps the row visible with its reason. A refused row never runs on default rules and is
  never sent to the home endpoint as an unlisted id.
- Call records and model receipts name a model's contract by its switches
  (`replay_reasoning=always strict_finish …`) where they carried the contract's name.
- MiniMax native search is allowed for a model whose row has the `search` role (M3.1 Flash
  has it) instead of for one model named in code.
- **One `image` config key** replaces `image_route`, `image_provider` and `image_cli`: `auto`
  (default: deepseek-flash on the home key, else the first keyed vision endpoint, describes
  pasted images for a text-only model), `ask` (asked before a provider describes), `off`, an
  endpoint spec that pins the describer (`kimi:kimi-k3`), or your own command (`mmx`, or a
  template with `{path}`). A config written with the old keys is read as the one key. A
  pinned endpoint that is not keyed sends the placeholder instead of silently using another;
  `auto` no longer prefers a configured CLI — name the command to use it.
- `/queue on|off` sets how a message sent mid-turn is delivered (off = interrupt, the
  default; on = it waits its turn); bare `/queue` shows the mode and the waiting messages.
  The separate `/delivery` command (never in a release) is gone: one feature, one command.
- `/proposals` and `/routines` are listed in `/help` and completion only while `evolve = auto`
  or that inbox has something waiting at launch; typed in full they still open. The hints
  line no longer advertises `/proposals`.
- **Dream remembers what actually went wrong.** An episode note used to keep the reflect
  grade and the approaches that failed. It now also keeps a `## failure signals` section
  when there are any: the grader's reason for a low score, and what the harness itself
  counted (tool errors, engine errors, interrupts mid-work, test runs that did not pass).
  A clean session adds nothing. Only relevant while `evolve = auto`.
- **The language-server bridge stops pretending** (it stays experimental and opt-in).
  A query that failed now says so: a stopped server, an operation the server does not
  have, a server error and a timeout each come back as `[error]` or `[timeout]` and count
  as failed tool calls, where they used to read as `[no results]` or as a success.
  `[no results]` now only means a query that worked and matched nothing. Diagnostics no
  longer ride on `read_file`: every read waited on the server, a refused read still asked
  it about the path, and a dead server looked like clean code. `check_code` runs lint and
  type checks after an edit with no server, and `lsp_diagnostics` stays as an explicit
  query. `/lsp` tells an unconfigured user how to set it up instead of saying it is off,
  and `ROCKYCODE_LSP_COMMAND` is parsed as a command line (quoted paths work; unbalanced
  quotes are reported, nothing is launched).
- **One rule for which key and URL a model uses**, in one place (`engine/route.py`), for
  every way rocky talks to a model: chat, `/model`, `exec`, `goal`, `dream`, `bench`, the
  MCP server, the desktop, titles and image descriptions. A model goes to its own endpoint
  with that endpoint's key. **A model whose key is missing is no longer sent to the home
  URL with the home key** (where it failed with that provider's unrelated error): rocky
  stops and names the variable to set. The one exception is the setup you build yourself:
  with `ROCKYCODE_BASE_URL` on your own gateway, a model without its own key still rides
  the gateway. A plan-only model never does. An id rocky does not know still goes to the
  home endpoint, as before.
- **The first run asks where your models come from**: any provider in the catalog (plan
  rows too), a local Ollama with no key at all, or your own URL. It used to ask for a
  DeepSeek key whatever you were about to run. The key is tested against its own endpoint
  and saved under its own name, and the default model is one of that provider's. When a
  model you named only lacks its key, rocky asks for that one key and carries on. With no
  terminal to ask in (a script, CI) it prints the one line and stops instead of waiting.
- **Structure.** `rockycode/engine/` is the model- and UI-agnostic core and never
  imports upward — a smoke (`tests/smoke_layering.py`) enforces it. Goal mode is its own
  package (`rockycode/goal/`); the `exec` and `serve` hosts are `rockycode/headless.py` and
  `rockycode/server.py`; the verifier is one module (`engine/verifier.py`; the `engine.jev`
  shim is gone). The dynamic-workflow spike left the package and the CLI
  (`experiments/dynamic-workflows/`).
- **One approval pipeline** (`engine/approval.py`): static policy → session grants → loop-tick
  rules → verifier → the host's ask. The TUI, `serve` and `exec` are hosts of the same
  pipeline; `serve` clients now get the yolo/ask/careful policy, session grants and the
  verifier, and safe reads no longer prompt. `session/request_permission` carries `detail`
  and `warning`. The desktop host rides the same `ToolApprover` per task (it kept its own
  copy of the policy before): verifier included, and a decision the pipeline takes on its
  own is marked on the tool row.
- **Reflect is a boundary hook** (`dream/reflect.py`, attached from config); the engine keeps a
  plain hook list and no longer knows evolve.
- **TUI shell split** per concern under `rockycode/tui/`; `tui/commands.py` is the one slash
  command table that drives `/help`, completion and dispatch (`/queue` now
  autocompletes). The CLI is a package with one module per command.
- **UI text is one TOML file per language** (`rockycode/i18n/locales/en.toml`, `zh.toml`), loaded by
  `rockycode/i18n/`. Keys are meaning-named (`app.loop_paused_approval`, not the English sentence),
  colours are palette placeholders (`{violet}`, `{muted}` …) instead of hex literals, and
  `ui_language` accepts any locale file present. Adding a language is adding a file;
  `tests/smoke_i18n_catalog.py` checks parity. Runtime capability details are reported with a
  `detail_key` + `params` beside the English `detail`, so clients can localize by key.
- **The desktop reads the same locale files.** Its text is the `[desktop]` section of each TOML file,
  exported to `apps/desktop/src/shared/catalog.ts` by `scripts/i18n_desktop.py` (checked by the
  catalog smoke); capability details are localized by `detail_key`, and the language picker lists
  every exported locale. One TOML file per language now covers both surfaces.
- **No sentence maps anywhere.** The desktop host's refusals, persisted task and race-lane
  errors, runtime limitations and the model-race panel's copy are catalog keys
  (`diagnostic.*`, `race.*`) in the same TOML files; the renderer localizes by key and shows
  the English text only for a key it does not know. The Electron main process throws
  already-translated messages. `TurnDelivery.submit` raises `DeliveryError` with a stable
  `code`. The last hard-coded TUI hint line reads the catalog.
- **`serve` says what the approval pipeline decided on its own.** A static block or the
  verifier's auto allow/deny is a `session/approval_note` notification (`kind`, `tool`,
  `detail`), so editor clients can show it the way the TUI does; unknown notifications are
  ignored by existing clients.
- **Evolve is opt-in.** `evolve` defaults to `off`. `/config evolve auto` turns on the
  reflection checkpoints and the launch catch-up dream, with a one-time disclosure on the
  next launch. Every evolve call bills the session model, which is why it no longer starts
  on its own. Evolve itself is v2: checkpoints run on the session's own model at turn,
  compaction and exit boundaries (capped per session), the dream pass works over those
  reflect records, proposals are quality-gated and approved skills are watched; the
  Ollama-only judge/mining pass and the exit sheet are retired.
- MiniMax's shipped base URL is `https://api.minimax.cn/v1`. Current China
  docs (platform.minimax.cn and platform.minimaxi.com, checked 2026-10-20)
  publish that host; the previous `https://api.minimaxi.com/v1` is no longer
  the documented endpoint. `minimax-m3` uses the same host.

- **Goal mode forgets between milestones.** Each milestone (and each retry after a failed verify) now starts from the goal's seed context instead of the whole run's history; the work stays in the goal worktree/branch, and a retry is told why the last verify failed. Long runs no longer fixate on an earlier attempt (context inertia). Opt out with EngineDriver(flush_context=False).

### Fixed
- **Quitting while rocky works leaves nothing behind.** ctrl+q or `/exit` in the middle of a
  turn used to close the screen under the turn, and the TUI ended with "Task exception was
  never retrieved … MountError". Now the first ctrl+q stops what still runs, the way Esc
  does, and then leaves: a reply half written, a tool running, an approval card waiting, a
  `/loop` tick, a `/plan` turn. A command that waited for your yes does not run. What you
  type while rocky is leaving is not sent, and rocky says so. rocky waits at most 3 seconds
  for the stop, and a second ctrl+q or `/exit` still leaves at once.
  With evolve on, the final reflect now sees a stopped turn, and a loop tick no longer goes
  on working while rocky leaves. Found live by dy2012 (#144).
- On Python 3.14, stopping a turn (Esc, or leaving) no longer makes the event loop report
  "WorkerCancelled exception in shielded future". Nothing showed on screen: the TUI hides
  what is printed while it runs. It showed in headless runs, and it failed the tests.
- **A turn stopped at the wrong instant no longer ends rocky.** Two old bugs, each needing a
  second key within a few thousandths of a second. Esc, or a new message, in the instant a
  reply finished drawing ended the TUI with "MountError … because it has no parent". Enter
  on an approval card with Esc right behind it closed rocky without a word. Both came from
  stopping a turn while it waited for a widget to be removed; every such wait is now safe to
  stop (`tui/removal.py`). With evolve on, a final reflect no longer starts after a second
  ctrl+q has left.
- The exit card ("session saved", how to resume) now speaks the UI language: Chinese when
  `ui_language` or `lang` is zh. It was English in every language. The commands on it stay
  as you type them.
- **A card no longer takes your typing as its answer.** A card that opened while you were
  typing took the keyboard: Enter or `y` ran a command you had not read, and the rest of
  your sentence was dropped. Every card in the TUI's chat now follows one rule (an approval,
  a parked step, the plan review, the image route, `/loop`, `/proposals`, `/routines`).
  A card you did not open never takes the keyboard. It waits, dimmed, and its last line
  says how to come to it: Enter in an empty message box, Tab, or a click. ↑ and ↓ in an
  empty message box do nothing while a card waits; if you pressed them, the card has no
  picked row when you come to it, and Enter picks nothing until you choose a row on the
  card. With two cards on screen, Tab goes round them, and each waiting line names the key
  that leads to its card. A card you opened
  yourself (`/proposals`, `/routines`, `/loop`, `/loop allow`, `/plan`, a parked step) has
  the keyboard at once. No letter answers a card: `y`/`a`/`n` and `g`/`e`/`p`/`s` are gone,
  and you answer with ↑↓ and Enter or by clicking a row (the plan's and the image card's
  rows are clickable now too). What you type or paste on a card goes into the message box.
  Enter picks a row only after the card has had the keyboard, untouched, for half a second;
  an Enter that comes sooner says so on the card and starts the wait again, so the Enter
  that brought you to the card, or one held down, picks nothing. The arrows act at once.
  A call that carries a warning starts on Deny, and ↑↓ stop at the first and last row
  instead of wrapping around. Esc in the message box is the Esc of the card you were last
  on. When a card closes, the keyboard goes back to the message box, never on to another
  card. So an approval now takes two Enters, one to come to the card and one to pick the
  row, or one click. A message sent while rocky works still takes over the turn by default, so the call
  that was asking is dropped with it, never run; with `/queue on` the message waits and the
  card stays. In the desktop app a call that carries a warning is allowed by its button
  only: Enter and S do nothing on it. The goal screen keeps its `y`/`e`/`n` keys: it is a
  screen of its own, with no message box behind it.
- **The line that opens the parked steps is always on screen.** After a turn that parked a
  step, the transcript could stop one row short and hide that line under the border, about
  once in a hundred turns.
- **Typing a slash command.** The suggestion row froze on its first content (`/p` kept showing
  the same four commands whatever came next, and "no match" could never appear). Tab with
  several matches moved the focus out of the input, and what was typed after it was lost. The
  row now follows every keystroke, a bare `/` lists the commands, and Tab completes as far as
  the matches agree and never leaves the input.
- Nothing moved between Enter and the model's first token: the status only changed when the
  first delta arrived, so a slow network looked like a frozen app. "thinking…" now shows at once.
- `/clear` empties the screen and nothing else, while the half-context reminder told users to
  "type /clear to start fresh". `/clear` now says the conversation is kept, the reminder names
  what does start fresh (`/task new <note>`), and the context-full error no longer suggests a
  compact or clear command that does not exist.
- `/help` lost `[objective]`, `[key] [value]` and `[id]` to the markup parser, and opened
  scrolled to its last lines. The arguments are shown and the card opens at its top.
- `rockycode --help` and every subcommand's help came in ANSI cyan, yellow and green, first-run
  and exit text was auto-highlighted in more of the same, and a few lines named `[cyan]`,
  `[magenta]` or `[yellow]`. All CLI output now takes its colours from the palette.
- A skill whose description is a YAML block (`description: |` or `>-`) was listed as `|`, in
  `/skills` and in the model's own skill list. The text under the indicator is the description.
- `/goal` opened its screen in English in a Chinese session.
- **Desktop: Enter on a focused "Deny" allowed the action.** The approval card's global Enter
  ("allow once") also fired when a button had the focus, so Tab to "Deny" and Enter granted what
  the user meant to refuse. A focused control now keeps its own Enter, and the card's keys do
  nothing while a dialog is open.
- Desktop: the hint under the composer said "⌘ / Ctrl + Enter to send · Enter for a new line"
  while Enter sends. It now says "Enter to send · Shift + Enter for a new line".
- Desktop: a new task starts in "Approve each step" when the global config, or the project's,
  says `permission = "careful"`. It always started in "Standard". A config never starts a
  desktop task in auto-approve.
- Desktop: "ready" now means a model can run. It meant "a default model name is saved": a
  user with a working key and no saved default was told to configure, and a default without a
  key said ready and failed on the first message with terminal text. A new task now starts on
  the saved default when it has a route, else on the first model that has a key or runs
  locally. When nothing can run, the start page and the composer show one card: the step, the
  exact terminal command for this install, and "Check again" (no relaunch). A task whose own
  model has no key says so in one sentence and points at the model picker.
- Desktop: a user with no MCP server saw the raw key `capability.mcp_none`; a duplicate preset
  name was reported in Chinese to English users and an empty preset as `diagnostic.presetBody`;
  the mode help said rocky has no reviewer (the opt-in verifier exists); the limits list said
  there are no embedded previews (there are); a missing `uv sync` was reported as a missing
  checkout.
- The desktop host tests passed only on a machine with a real key file and failed on a clean
  clone. They now run on one fixture key and never read the developer's key file or keychain.
- Small side calls — session titles, the goal reviewer, dream, bounded completions for
  hosts, the `raw` bench runner — capped their output with `max_tokens` even on models whose
  row says `max_completion_tokens` (Kimi K3.1 Pro, Qwen4 Max Preview, MiMo-V2.6-Pro). They now
  use the same field as the main loop.
- A model row with a malformed `price` crashed the catalog load; the row is refused with a
  reason like any other bad row.
- The cost line now includes DeepSeek's native-search fee (USD 0.75 or CNY 5 per 1,000
  successful searches, charged since November 2026). Each search response's count of
  successful searches goes into the ledger; attempts and source counts do not. When a count
  is missing or a request is cut off, the total stays a lower bound and the TUI marks it
  `(search fees incomplete)`. A custom gateway is not priced at the official rate, and
  MiniMax search fees are still outside the ledger.
- **Desktop dark mode is whole.** On a dark OS the Model Race view stayed light (its button
  was a white block with unreadable text), the delivery dropdowns were white, and several
  lines and labels kept their light-page colours. Every colour that only works on a light
  page now has a dark value, native controls follow the dark scheme, and a test fails when
  a new one is added without one.
- `ROCKYCODE_HOME` moves everything rocky owns. The settings file (`config.toml`), the price
  overrides (`pricing.toml`) and the goal logs stayed under `~/.rockycode` whatever the
  variable said, so a relocated store was split in two. If you set `ROCKYCODE_HOME`, move
  those two files and the `goal-logs` folder into it.
- **Goal mode runs on the model you chose.** `rockycode goal` and `/goal` built their own
  client from the home key and URL and spoke DeepSeek's request shape, whatever the model:
  on any other provider the plan, the work and the review were all sent to the home
  endpoint. Each now uses its model's own endpoint, key and shape, a reviewer on another
  provider gets its own connection, and `/goal` keeps the session's plan or custom
  endpoint.
- **`explore` speaks its parent's model.** The read-only child rode the session's client
  but sent the default model id and reasoning fields, so on a model whose API id or
  thinking shape differs (MiniMax, Qwen, Kimi, a local server) its requests were wrong.
- `exec` no longer demands the home key for a task on another provider or a local server,
  and neither does the first launch: `rockycode --model <a model you have a key for>`
  starts without asking for a different provider's key.
- An authentication error names the key and URL that were really used. It used to name the
  home pair on every provider. The session record's `base_url` is the real one too.
- `--model ollama:<model>` with the local server down says so at launch instead of sending
  the id to the home endpoint.
- **Saving a key no longer rewrites the key file.** First-run setup replaced
  `~/.rockycode/.env` with three lines of its own, deleting every other provider's key and
  any line you had added. A key is now written as its own line and the rest of the file is
  kept; the file is `0600` from its first byte and is swapped in whole, so an interrupted
  save leaves the old file.
- `bench` with a model that has no key stops with one line before Docker and the dataset.
- MCP calls: cancelled queued calls are skipped at dispatch, and new calls fail fast
  once shutdown starts. Graceful stop still serves previously accepted calls; forced
  stop, actor failure and connection loss settle active and queued waiters instead of
  leaving them pending. Repeated/concurrent stop is safe, including a cancelled stop
  waiter. Cancelling a caller after dispatch still permits server-side effects; this
  is admission control, not rollback (host-contract research #74).

- DeepSeek catalog notes now identify Flash 1017 and the still-available Pro
  0813, with source dates. The bounded `python -m rockycode.model_probe` checks
  alias equivalence; `--live` explicitly compares the two literal API ids using
  the actual Engine. It retains Flash's protocol and 393216 output cap.
- Local TUI/desktop YOLO reads now share a live file scope across read_file,
  grep, glob and image inputs; default headless registries remain bounded.
  Search checks symlinks before traversal and preserves secret-file filters.
  Image tools ignore project-controlled CLI routing, and container read-file
  diagnostics treat filenames literally. File-tool write scopes are unchanged.
- Tool batches on every provider now require a matching terminal completion
  marker before any tool runs. MiniMax M3.1 Flash retains interleaved reasoning
  across tool turns, resume and compaction. Dream/title requests respect model
  wire ids, thinking policy and output budgets; truncated auxiliary replies are
  rejected. The dream CLI uses the selected keyed endpoint. The bounded canary
  supports `--model minimax-m3.1-flash` (see `docs/minimax-m3.1-flash.md`).
- **DeepSeek Flash 1017 contract**: Flash assistant messages keep their
  `reasoning_content` and replay it on tool turns, including after a restart or
  compaction (opt-in per model via `protocol` in `models.toml`; other models are
  unchanged). Incomplete or errored streams never execute a partial tool batch.
  Flash output cap corrected to 393216. `rockycode bench` and the raw runner now
  default to the registry's output cap and context window, like chat; pass
  `--max-tokens 16384` to keep the previous bench budget. Live canary:
  `python -m rockycode.flash_probe --live` (see `docs/deepseek-flash-1017.md`).
- Release builds pin upstream Actions and build dependencies, separate OIDC
  publishing from validation, verify reproducible distributions and installed
  model protocols, and keep private development files out of release artifacts.
- Jev auto-mode preserves human-only read-jail grants, redacts before clipping,
  fails closed on truncated arguments and malformed probabilities/URLs, and
  preserves loop refusal receipts. Ordinary config writes retain `[jev]` settings.

- Sidecar and race clients send the provider's case-sensitive API model id
  (`provider.api_model()`); selection, the ledger and pricing keep the canonical registry id.
- The MCP metadata-store guard inspects only the path components below the trusted root,
  so a symlinked `/var`, a dotfiles-managed `~/.rockycode` or a symlinked `$HOME` no longer
  refuse the store.
- `bench`: a fix left in a stash no longer yields an empty patch, binaries and model-authored
  test files are dropped from patches, retries happen only after crashes, and scratch files
  go to `/tmp`.
- TUI: the loop tick stays active through fold and receipt rendering; `exec --help` renders
  correctly on dumb and colour terminals.
- Desktop: `npm run setup:electron` survives the macOS bundle rebrand, keeps the installer's
  native path on Linux and Windows, and exits with the installer's own status instead of
  masking it. The race, delivery and live e2e launchers mark the unpackaged dev layout
  (`ROCKYCODE_DESKTOP_DEV=1`) like the main e2e does, so they find the Python host after the
  dev Electron binary is rebranded instead of reporting a disconnected runtime.

- MCP tool results flagged isError are capped like every other tool output (30k chars, head+tail) instead of entering history unbounded.

- `/model`: a custom base URL stays editable on later opens (the saved `<provider>-custom` row reopens the field pre-filled, and a keyed model's other endpoints stay in the short list).
- MiMo V2.6 Pro keeps its thinking across turns: `reasoning_content` is replayed with and
  without tools, the output cap goes out as the documented `max_completion_tokens`, and the
  context summary is requested without tool schemas (the server may ignore
  `tool_choice="none"`). The shared summary step now refuses a result that did not finish
  with `stop`, carries tool calls or is empty, and prunes old tool outputs instead of
  replacing history with it. Live-checked on the MiMo Token Plan (Singapore cluster) and
  against DeepSeek Flash; pay-as-you-go MiMo was not exercised.

### Removed
- The `openrouter-laguna-s` row: OpenRouter no longer lists that free model.
- The `reflect_every` config key (never in a release). The reflect checkpoint interval is
  the constant `REFLECT_EVERY` (200 history messages); nobody tuned it.
- `rockycode.tui.rocky_pet`, the sprite widget nothing has mounted since 0.1, and the prompt
  hint for a `viewport` tool that does not exist.
- The dynamic-workflow spike (`rockycode workflow`: phases with fan-out and per-phase
  `on_error`) moved to `experiments/dynamic-workflows/` and is not part of the package.
- The `/cron`, `/artifacts` and `/quit` aliases. `/loop`, `/artifact` and `/exit` are the
  commands; each alias cost a help line and a doc line and added no capability. An old alias
  now gets the ordinary unknown-command reply.

### Security
- Python 3.15 works: seven pinned sub-packages moved to versions with ready-made wheels for it
  (cffi, jiter, pydantic, pydantic-core, pywin32, typing-extensions, typing-inspection). pydantic
  2.14.2 also fixes two validation bypasses (GHSA-qpmf-fgh3-ff25, GHSA-3r8g-cqwr-jr29).
- **Every sub-package is pinned.** Installing rocky used to take the newest version of each
  indirect dependency that day. The package now pins the whole tree, for the default install
  and for each extra, to the versions its own tests ran with. The VS Code extension's direct
  packages and the CI actions are pinned too. A security fix in a sub-package now needs a
  new rocky release. Installing rocky into a shared environment can conflict with other
  versions there: `uv tool install rockycode` keeps it in its own.
- **A project folder can no longer choose a command that rocky runs.** The `image` setting
  can name a program that describes images for a model without vision. rocky read that
  setting from the project's `.rockycode/config.toml` as well as from your own, so a cloned
  repository could name the program, and sending a pasted image from the terminal ran it
  with no question asked. `image` is now read from your own `~/.rockycode/config.toml` only, like
  `model`, `vision_models` and `search_max_uses`; a project file can still set its language
  and make approvals stricter.
- **The image command starts without rocky's keys.** It inherited rocky's whole environment,
  provider keys included. It now gets what the shell tool gets: your environment minus
  rocky's keys. A tool with its own login (`mmx`) is unaffected. The `git` commands rocky runs
  itself in a project (goal branches, the explore child) get the same environment.
- **A change to rocky's own settings always asks you.** The `rocky_config` tool can change
  the default model, give a provider another address (the key saved for that provider goes
  to it) and add a model server. Such a change used to run silently in yolo, and after one
  "allow for this session" every later change ran silently too. Now every change is yours
  to decide, in every approval mode, each time (in yolo it is kept for you instead of
  asked: see "yolo does not stop to ask" below); there is no "allow for this session" for
  it; the optional decision model never answers for you. The card says the change in plain words and in full
  (the terminal's card showed raw JSON, cut at 400 characters), and warns when a saved key
  would go to a new address. `exec` is unchanged: it still needs `--allow tool:rocky_config`.
- **Three settings rocky does not change for you, even when you approve.** A command as
  the `image` describer, a looser approval mode (ask → yolo), and a model server added under
  a built-in provider's name (that provider's key would go to the new address). The first
  two you type yourself: `/config image <command>`, `/config permission yolo`. For a
  built-in provider's own address there is `set_url`, which asks and carries the warning.
  The built-in `rocky-setup` skill says so, and now points to the hidden key field instead
  of the key file.
- **A shell command that names rocky's keys or settings asks every time.** That is the key
  file, the four settings files (`config.toml`, `models.toml`, `endpoints.toml`,
  `providers.toml`) and the folder as a whole (`~/.rockycode`, `$ROCKYCODE_HOME`, its real
  path, a wildcard over it), plus commands that read a password from the OS keychain
  (`security find-generic-password`, `secret-tool lookup`, `keyring get`). What else lives in
  the folder is ordinary work: a global skill's script, an old session, a saved plan. The
  rule holds in yolo, is never covered by a session grant, and in
  `exec` needs its own grant (`--allow rocky-store`, `--allow keychain-read`). `write_file`
  and `edit_file` refuse rocky's settings files, which a session started in your home
  directory had in reach. This stops the plain attempt and the honest mistake. It reads the
  command's text, so a command that builds the path another way is not caught: for a folder
  you do not trust, the sandbox is the boundary, not yolo.
- **yolo does not stop to ask.** The steps above, and `lake … --inadvisably-no-sandbox`, are
  yours to decide in yolo too. A card for one of them waited with no time limit, so a long
  run stood still until you came back (in the desktop app the card answered "deny" by itself
  after five minutes, and the step was gone). yolo now refuses such a step at once and keeps
  it for you. rocky is told to go on without it and not to reach it another way. In the
  terminal:
  - The status bar counts what waits (`⏸ 1 waiting`), the turn leaves one line you can
    click, and `/parked` is the typed way.
  - The card opens only when you ask for it, so a key typed for your next message cannot
    answer it. Its rows are **Run it now**, **Don't run it** and **Decide later**; Enter on
    the default decides nothing.
  - **Run it now** tells rocky to pick the step up: at once, or after the turn that is still
    running. A pass for exactly that step exists only while that message's turn runs, so a
    message you drop from the queue allows nothing, and its step waits again.
  - The third such step in one turn stops the turn. The rest of that batch is not run.
  - A card that needs an answer takes the keyboard: an open waiting-list card closes and the
    step stays parked.
  - A `/loop` tick in yolo used to drop such a step without a trace. It joins the same list.

  In the desktop app the steps wait in the task, above the message box, each in plain words
  with **Run it now** and **Don't run it**. They have no timer and no key answers them. The
  list lasts for this run of the app.

  ask and careful are unchanged: the card comes at once. `exec` is unchanged (the run ends
  and names the grant it needs), and so is `serve`. On every approval card in the terminal
  the choices can now be clicked.
- **A key is saved only where you said.** When you chose the OS keychain and it did not take
  the key (locked, an ssh session), rocky wrote the key to the plain-text key file and told
  you afterwards. Now nothing is saved: the terminal's key field stays open with the key in
  it, the first run asks again, the desktop form says so, and the key file is a choice you
  make. And "your OS keychain" now means the operating system's own store (macOS Keychain,
  Windows Credential Manager, Secret Service, KWallet): with a plain-text or third-party
  `keyring` backend installed, rocky uses its own 0600 key file and calls it that.
- **The desktop's HTML preview no longer runs scripts.** A page the model wrote used to run
  its inline scripts inside the preview panel. The panel was already cut off from the
  network, the app and every other page; what changes is that model-written code no longer
  starts on its own inside the app. The panel now shows a static page: styles and embedded
  images stay, scripts and inline event handlers do not run, and a page cannot switch them
  back on from inside. The file on disk is untouched, and the arrow button opens it in your
  browser, where its scripts run as in any page you open yourself.
- **A key pasted into the chat input is no longer kept.** The up-arrow history
  (`~/.rockycode/prompt-history.jsonl`) stored every typed line as typed, in a file created
  world-readable (0644). A line is now kept with key-shaped text masked (and the
  value of any key-named variable rocky can see), in the up-arrow list and on disk; a file from
  before is cleaned the next time rocky starts; the file is 0600. What is sent is unchanged:
  only what is kept is masked.
- **Rocky's keys no longer ride in the shell tool's environment.** A local `bash` command
  (and `! command`) used to inherit everything rocky loads from its store, so a command, a
  test runner or a dependency that dumps the environment could read and send a provider
  key. The shell tool, the code checks, and MCP and LSP servers now start with your
  environment minus what rocky loaded from `~/.rockycode/.env` or the keychain, and minus
  rocky's own key variables (`ROCKYCODE_…_API_KEY`) even when you exported one yourself.
  Your own exports still reach the shell tool. A variable your commands need belongs in your
  shell, not in rocky's file; a nested `rockycode` loads the store itself. The local shell
  still runs as you and can open files you can — the sandbox is the boundary for that.
- A session grant for the `lake` binary does not cover `--inadvisably-no-sandbox`
  on `lake check` or `lake comparator`. That flag asks every time, including in
  yolo, and Jev cannot auto-allow it. Goal and headless treat it as ask-tier
  (`lake-no-sandbox`) until that grant is given explicitly.
- aiohttp 3.13.5 → 3.14.5 (GHSA-5wr8-gcqf-gqf3): a connection that never sends its first
  request, or stalls mid-header, is now closed at the keepalive deadline. The artifact
  preview server (`engine/artifact.py`) runs on aiohttp; it listens on loopback and gates
  every route behind the session token, but the token is checked after parsing, so idle
  sockets were unbounded. `tests/smoke_artifact.py` keeps a bounded two-connection
  regression. yarl follows to 1.25.1; nothing else in the lock moves.
- The optional PDF extra now requires `pypdf>=6.21.4` (lock 6.14.2 → 6.21.4): seven
  moderate advisories in earlier versions. The 6.21.4 follow-up fixes incomplete-xref
  trailer loops, shared-resource page-tree memory growth, and DictionaryObject clone loops
  (GHSA-5849-834g-hvw5, GHSA-cggf-277g-3q54, GHSA-mfr5-77xh-9j7h), in addition to the
  four issues fixed by 6.21.3 (GHSA-42gh-23hr-vphw, GHSA-4vqv-7265-gm2w,
  GHSA-62m5-6wh8-28x7, GHSA-x3rh-gjgm-fmqc). Nothing else in the lock changes; the core
  install does not include pypdf.
- Desktop lockfile: `undici` 7.30.0 → 7.30.1 (GHSA-xjj6-f33q-929c), an optional dev
  dependency of `@electron/get`. The pinned install never loads it (`setup-electron.mjs`
  does not enable the proxy downloader, and the repo's Node 24 bundles 7.30.1), so this is
  preventive; `dependency-baseline.json` carries the new lock hash. Verified with a fresh
  `npm ci --ignore-scripts`: dependency policy, audit and the unit tests pass.

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
