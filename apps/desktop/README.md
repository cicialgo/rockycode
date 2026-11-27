# RockyCode Desktop (preview)

[中文说明](README.zh-CN.md)

An Electron + React/TypeScript workbench over the same Python core as the
terminal UI. Pick a folder, run tasks with streaming output and inline tool
approvals, keep per-task notes, preview HTML results, and race five models on
one task. The agent loop stays in the Python core; the desktop adds a window on the same engine.

**Status: source preview.** It runs from a clone of this repository. An installer, code signing and auto-update come later. macOS is the platform it is used on day to day; Windows and Linux build and pass the fixture checks in CI, and still wait for a check on real machines.

## Run from source

Requirements: Node.js 24 (see `.nvmrc`) and Python 3.11+. You add a model key
in the app itself: the first-launch card has a form. The terminal's first run
(`rockycode`) is the other way in, and both use the same store. The desktop reads RockyCode's user-level configuration only: a project `.env` is ignored, and keys stay hidden in the interface.

```sh
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv sync                      # the Python core and its dependencies, in ./.venv
cd apps/desktop
npm ci --ignore-scripts      # exact lockfile; install scripts stay disabled
npm run check:deps           # registry, integrity, pins, known-bad versions
npm audit signatures
npm run setup:electron       # fetches the pinned Electron binary once and verifies its checksum
npm run dev
```

`npm run dev` builds and launches the app in the development layout: the core is
this repository and the interpreter is `./.venv/bin/python3`. For any other
layout point `ROCKYCODE_PYTHON` at a Python 3.11+ interpreter that has
`rockycode` installed, and `ROCKYCODE_SOURCE` at a source checkout when it is another one:

```sh
ROCKYCODE_PYTHON=/absolute/path/to/python3 npm run dev
npm run dev:fixture          # offline interaction mode: simulated model and tools
```

The fixture mode is clearly labelled in the window. It exercises the protocol
and the approval flow with simulated events; a model acceptance test needs a real model.

## What the app does

- **Tasks in a folder.** You choose the working folder through the system
  picker before anything runs. Each task keeps its own draft, notes and model.
  Stopping is immediate; a task interrupted by a crash is marked as such and waits for you to run it again.
- **Keys.** The first-launch card has a form: pick where your model comes
  from and paste the key into a hidden field. The app tests the key with one
  free request, saves it to the system keychain where there is one (or to the
  0600 key file), and is ready at once. The same form sits in the
  runtime dialog ("Add or replace a provider key") for later keys.
- **Approvals.** Tool calls go through the same policy as the terminal UI, per
  task: `ask` (safe reads and in-folder writes pass, risky tools ask), `careful`
  (every risky or moderate call asks) and `yolo` (everything but block-tier
  commands). In `yolo`, a step only you can allow waits in the task, above the
  message box, with **Run it now** and **Don't run it**. "Allow for this session" covers that one shell binary only. Starting an external MCP or LSP process is a separate consent,
  shown with the server name, its config source and the command. Details:
  [docs/TASK_UX.md](docs/TASK_UX.md) (Chinese).
- **HTML preview.** HTML results open in a sandboxed panel on the right; local
  HTML links in replies are clickable; an arrow opens the file in your browser.
- **Model Race.** Five configured models, one task, five isolated runs, human
  scores and an optional anonymous model judge; a full local JSON preview before any export, and the file stays on your machine. See [docs/MODEL_RACE.md](docs/MODEL_RACE.md).
- **Update reminder.** A banner says when a newer desktop version exists, with
  a button to the download page and "Later". You decide when to download and install. The check runs at launch, at most once a day; the sidebar has the
  off switch and a "Check now", and the app menu has "Check for Updates…".
- **Interface language.** The sidebar switches between system default, English
  and Chinese; the change applies to the interface, accessibility labels and the
  native menu, and survives a restart. Prompts, model output, tool data and your notes stay as they are.
- **Theme.** Two buttons in the sidebar: **System** follows your OS light or
  dark setting, **Nebula** is the terminal's own violet-on-night palette. One
  click switches, the choice survives a restart, and the app opens in it from
  the first frame.

Still to come: scheduled loops, the dream and goal interfaces, and full parity with the terminal UI. A registered tool still depends on its external service, which is verified separately.

## Prompt presets

The **Presets** control beside the task input saves a name and a prompt body: save the
current draft, create, rename, edit or delete. The list is shared by every task and kept
across restarts. Using a preset **replaces** the task input, and asks first when the input
holds different, non-empty text. A preset holds only a name and prompt text.

Storage is `prompt-presets.json` in the desktop user-data directory (file mode 0600,
directory 0700), next to the task database, apart from the project and apart from the `ROCKYCODE_HOME` that holds model credentials.

## Privacy

The app's only network request of its own is the update reminder: a GET of a
small version file, `https://rockycode.ai/desktop/latest.json`, at launch and
at most once a day; the request carries only the fetch itself. Switch it off in the sidebar. Model calls go only to the provider you configured. Analytics and crash reporting are absent by design. Interface preferences and the reminder's state live in the
app's user-data directory; task history lives in a local database there,
separate from your project files.

## Languages

Interface text comes from the same locale files as the terminal UI:
`rockycode/i18n/locales/<code>.toml`, section `[desktop]` (the `desktop.` prefix
is dropped in the app) plus the shared `[capability_detail]` section.
`python scripts/i18n_desktop.py` at the repository root exports them to
`src/shared/catalog.ts`, a generated file that is committed; rerun it after
editing a TOML file. Adding a language is adding a TOML file and rerunning the
export: the language picker lists it. Messages from the Python runtime and host
cross the wire as catalog keys (`error_key`, `detail_key`, a JSON-RPC error's
`data.key`) and are rendered in the active language; unknown keys fall back to
the English text they came with.

## Themes

A theme is a name in `src/shared/theme.ts` and a stylesheet. Nebula is
`src/renderer/nebula.css`: a token table at the top (the colours of
`rockycode/palette.py` and the terminal's `rocky` theme), then one line for
every colour the base stylesheets state. `tests/nebula.test.ts` fails when a
colour is added to `styles.css` or `race.css` without a Nebula value, so a new
component cannot show up in the wrong palette. Nebula is dark, so the main
process pins the colour scheme to dark while it is selected.

System's dark mode works by exception: the dark blocks in `styles.css` and
`race.css` restate only the colours that do not work on a dark page.
`tests/dark.test.ts` finds the ones nobody restated (a light fill, a light
line, text too dark to read), so a new component cannot ship as a white block
on a dark OS.

## Verification

```sh
npm run check                                   # dependency policy, tsc, build, unit tests
python3 -m unittest discover -s runtime/tests -v  # Python host tests (fixture; core tests skip without the core)
npm run test:runtime:shared                     # the same, forcing the shared core: Runtime + full host integration, no model calls
npm run test:e2e                                # real Electron window, offline fixture
npm run test:e2e:task
npm run test:e2e:race
npm run test:e2e:update                         # the update reminder against a local feed
npm run test:e2e:theme                          # the theme picker; writes artifacts/theme-*.png
```

Live checks against a real model are opt-in and cost API usage. They use
synthetic material in a temporary folder and isolate your Skills and MCP
configuration:

```sh
../../.venv/bin/python3 scripts/test-live-runtime.py --live
npm run test:e2e:live -- --live
```

## Dependency policy

npm lifecycle scripts are disabled for every install; nothing runs on
`npm ci`. Versions are exact, the lockfile is the source of truth, and
`npm run check:deps` checks every entry against the official registry, its
SHA-512 integrity, the pinned versions and a list of known compromised
releases. The Electron binary is downloaded only by the explicit
`npm run setup:electron` step, from the official release source, with its
checksum verified. First launch downloads nothing.

## Layout

```
apps/desktop/
├── src/main/        # Electron main process: window, IPC validation, Python host lifecycle
├── src/preload/     # the named methods the renderer may call
├── src/renderer/    # React UI (tasks, approvals, notes, HTML preview, model race)
├── src/shared/      # protocol types, i18n logic, the generated catalog
├── runtime/         # Python JSON-RPC host over rockycode.runtime (rockycode_desktop/)
├── scripts/         # dev/build/e2e launchers, dependency checks, Electron setup
└── docs/            # TASK_UX.md, MODEL_RACE.md
```

The renderer runs with `contextIsolation` and `sandbox` on and `nodeIntegration`
off; it only sees the methods exposed by the preload script. The main process
validates every call's origin and arguments. The Python host owns tasks,
approvals, history and the engine; stdout carries protocol only, stderr carries
diagnostics only.
