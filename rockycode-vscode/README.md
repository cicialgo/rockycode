# rockycode

**An AI coding agent, in your sidebar.** rockycode reads, edits, and runs code in your project — with streaming reasoning, inline tool calls, diff previews, and keyboard-first approvals. It's the VS Code companion to the [`rockycode`](https://github.com/cicialgo/rockycode) CLI and runs on DeepSeek (or any OpenAI-compatible model).

**Source:** [github.com/cicialgo/rockycode](https://github.com/cicialgo/rockycode)

## Features

- **Chat panel** in the activity bar — ask rocky to fix a bug, add a feature, or explain code.
- **Streaming** thinking + answers, with **syntax-highlighted** code blocks.
- **Inline tools** — every read / edit / shell command shows up as a collapsible card as rocky works.
- **Keyboard-first tool approvals** — when rocky wants to run something risky, approve it right in the panel: **Enter** to run, **↑ ↓** to choose, **Esc** to deny. The full command is always shown before you approve.
- **Send code to chat** — select lines and press **⌘K ⌘L** (or right-click → *Add Selection to rockycode Chat*) to pin them as a `@file:lines` reference. A live pill also shows the active file so rocky always knows what you're looking at.
- **Diff previews** for edits before they land.
- **Theme-native** — adopts your VS Code light/dark theme.

## Setup

1. **Install the `rockycode` CLI** (the extension drives it):
   ```bash
   uv pip install rockycode        # or: pipx install rockycode
   ```
   The extension auto-detects a project `.venv/bin/rockycode`; otherwise set **`rockycode.pythonPath`** to your rockycode executable.
2. **Open the rockycode panel** and paste your API key. It's stored in **encrypted VS Code Secret Storage** — never in plaintext settings, never synced. Get a DeepSeek key at <https://platform.deepseek.com/api_keys> (any OpenAI-compatible key works too).

That's it — rocky works in any project you open.

## How it works

The extension spawns `rockycode serve` and talks to it over JSON-RPC on stdio. Your key lives in Secret Storage (or `~/.rockycode/.env`, or the OS keychain via `rockycode[keyring]`); tool output and trajectories are redacted so secrets never leak.

## Commands & keybindings

| Command | Default keybinding |
|---|---|
| Open rockycode Chat | `⌘⇧L` / `Ctrl+Shift+L` |
| Add Selection to Chat | `⌘K ⌘L` / `Ctrl+K Ctrl+L` |
| Cancel current turn | `Esc` (while running) |
| New session | — |

## Settings

- `rockycode.model` — model ID (default `deepseek-v4-flash`).
- `rockycode.permissionMode` — `ask` (prompt on risky tools) · `yolo` (auto-approve) · `careful` (prompt on everything).
- `rockycode.pythonPath` — path/command for the rockycode CLI.
- `rockycode.autoInjectContext` — auto-attach the active file + selection to prompts (on by default).

## Security

- API key in **encrypted Secret Storage**, not settings; masked `🔑 ••••1234` shown in the header so you can tell which key is set without exposing it.
- Every risky tool call is **gated** — you see the full command and approve it explicitly (unless you choose `yolo`).
- Runs against untrusted repos with the same "assume hostile" posture as the CLI.

## Links

- Source & docs: <https://github.com/cicialgo/rockycode>

MIT licensed. amaze!
