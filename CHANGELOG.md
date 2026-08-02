# Changelog

All notable changes to rockycode are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/) — pre-1.0, so the surface may still
change between minor versions.

## [Unreleased]

_Nothing yet._

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
