# Contributing to rockycode

Thanks for wanting to help Rocky get there anyway. This is a small, opinionated
project, so a little context up front saves everyone time.

## What rockycode is trying to be

- **Its own agent, not a clone.** We borrow good ideas from Claude Code, Codex,
  and others selectively — but the design is our own, and *minimal* is the
  default. A feature earns its place; it doesn't get in because a bigger tool has
  it.
- **Small core, honest extras.** The base install stays lean. Anything heavy
  (SWE-bench, pandas, PDF, keyring) lives behind an extra. A PR that pulls a big
  new runtime dependency into the core needs a strong reason.
- **Security-conscious by default.** rockycode is built to run inside repos you
  don't fully trust. New code that touches shell, network, file paths, config
  loading, or credentials is held to that bar — see [SECURITY.md](SECURITY.md).
- **Measured, not guessed.** Behavior changes to the agent should come with a
  way to tell if they helped. The `bench` harness exists for exactly this.

## Getting set up

```bash
git clone https://github.com/cicialgo/rockycode.git
cd rockycode
uv pip install -e .            # or: pip install -e .
# extras as needed:
uv pip install -e '.[bench]'   # SWE-bench harness (heavy: docker, datasets)
uv pip install -e '.[keyring]' # store your key in the OS keychain
```

First run walks you through pasting an API key (DeepSeek or any
OpenAI-compatible endpoint). Then:

```bash
rockycode chat                 # interactive TUI
rockycode goal "…"             # autonomous run (sandboxed worktree copy)
```

## Before you open a PR

1. **Run the test suite** — `python tests/run_all.py`. It should be green.
2. **Lint** — `ruff check .` (config is in `pyproject.toml`).
3. **Match the surrounding code.** Read the file you're editing and mirror its
   naming, comment density, and idioms. New code should be indistinguishable in
   style from what's around it.
4. **Keep the diff tight.** One change per PR. No drive-by reformatting of files
   you didn't otherwise touch — it buries the real change in noise.
5. **Explain the why.** The PR description should say what problem this solves,
   not just what it does. If it changes agent behavior, say how you checked it
   helped.

## Reporting bugs and ideas

- **Bugs / features** — open a GitHub issue with enough to reproduce (command,
  OS, what you expected vs. saw).
- **Security vulnerabilities** — **do not** open a public issue. Use GitHub's
  private advisory flow; see [SECURITY.md](SECURITY.md).

## Licensing

rockycode is MIT-licensed. By contributing, you agree your contribution is
licensed under the same terms.
