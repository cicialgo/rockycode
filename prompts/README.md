# prompt lab

System prompt variants for A/B testing the rockycode harness.

`rocky-v1.txt` is a copy of the built-in `ROCKY_SYSTEM`
(`rockycode/prompts/rocky.py`) — the baseline. To test a variant:

```bash
cp prompts/rocky-v1.txt prompts/rocky-v2-strict-tests.txt
# edit it, then:
uv run rockycode bench --runner rockycode --tasks dev10 --prompt prompts/rocky-v2-strict-tests.txt
```

Each run records the prompt name + sha256 in its trajectory meta
(`.rockycode/trajectories/*.jsonl`), and predictions are written per-variant
(`results/predictions/rockycode-<model>-<prompt-name>.jsonl`), so runs never
clobber each other. Compare on three axes: score, steps per task, tokens per
task — a prompt that scores the same but uses half the steps is a win.

Since bilang (2026-07): the harness appends a generated `# Tools this
session` section to EVERY prompt (chat and bench) from the live tool
registry — do NOT hand-list tools in a variant file; the list would go
stale, which is exactly the drift the generated section removes. Chat
sessions additionally append language (config `auto|en|zh`), one
environment line, and the date stamp; bench appends none of those, so
bench prompts stay byte-reproducible. The recorded sha256 covers the
variant file only (the base), not the generated sections.

Ideas worth testing (one change per variant!):
- read-before-write rule vs. none
- forcing an exploratory first tool call vs. letting the model decide
- terse vs. verbose tool guidance
- Rocky's voice vs. neutral voice (does personality cost accuracy?)
- en vs. en+zh-closer vs. full-zh prompt (bilang phase 3 — does prompt
  language move score/steps/tool-call reliability on DeepSeek?)
