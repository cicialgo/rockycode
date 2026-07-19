# Changelog

## 0.1.0 — first release

The initial rockycode extension for VS Code.

- Chat panel in the activity bar, driven by the `rockycode` CLI (`rockycode serve` over JSON-RPC).
- Streaming reasoning + answers with syntax-highlighted code blocks.
- Inline tool cards (read / edit / shell) as rocky works.
- Keyboard-first tool approvals in the panel — Enter to run, ↑↓ to choose, Esc to deny; the full command is always shown.
- Send a selection to chat (⌘K ⌘L or right-click) as a `@file:lines` reference; a live pill shows the active file.
- Diff previews for edits.
- Theme-native (adopts your VS Code light/dark theme); pixel-note brand icon.
- API key stored in encrypted Secret Storage (never plaintext settings); masked `🔑 ••••1234` hint in the header.
- Resilient connection — auto-reconnect if the backend dies, project-venv auto-detection.
