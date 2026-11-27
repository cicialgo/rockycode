# prompt lab

System prompt variants for A/B testing the rockycode harness.

`rocky-v1.txt`, `rocky-v2-search-first.txt`, and `rocky-v3-decisive.txt` are
historical snapshots, not the current built-in `ROCKY_SYSTEM`. To test a variant:

```bash
cp prompts/rocky-v1.txt prompts/rocky-v2-strict-tests.txt
# edit it, then:
uv run rockycode bench --runner rockycode --tasks dev10 --prompt prompts/rocky-v2-strict-tests.txt
```

Each run records the prompt name + base SHA8 in its trajectory meta
(`$ROCKYCODE_HOME/trajectories/bench/`, default `~/.rockycode/trajectories/bench/`).
Prediction names include model, prompt, task set, and a minute-resolution
timestamp; identical runs in the same minute can collide. Preserve separate
run directories. Compare correctness, steps, and tokens, and use interaction
tests for tasks where producing a patch is not the requested outcome.

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
- en vs. en+zh-closer vs. full-zh prompt (bilang phase 3 — does prompt
  language move score/steps/tool-call reliability on DeepSeek?)

## Task-adaptation pilot (2026-10)

Rocky's identity and style are fixed in every arm. `rocky-zh-full.txt` mirrors
the shipped Chinese base. The pre-change EN/ZH baseline is frozen separately
in `bench/prompt_baseline_278a9dd.json`; other historical variants stay intact.

The new development pilot is a 2×2 factorial: broader task scope on/off ×
request-sensitive completion rules on/off. `scope_only` retains the old
forced-edit rules; `completion_only` retains the old bug-only job statement;
`adapted` contains both changes. These are semantic blocks, not single-line
ablations. The completion block also carries clarification and follow-up
rules, so this experiment cannot attribute their individual effects.

```bash
python3 -m bench.prompt_ablation --dry-run
# Explicitly billable: uses the configured model and a preinstalled Docker image.
python3 -m bench.prompt_ablation --live --reps 1
```

Default scope: four three-turn scenarios × two languages × four arms =
32 conversations / 96 user turns. `--reps 2` repeats the whole set. Every
conversation gets a fresh disposable workspace and a network-disabled
`ChatSandbox`; generated code never runs on the host. An already-installed
image is resolved to an ID before API calls; `--image sha256:...` can pin it
directly. Hidden acceptance checks run in a second fresh container after the
conversation, never in the model's context. Image setup does not download.

The pilot uses the real `Engine.run_turn` with its chat-default unlimited
step setting (no forced-edit budget reminders), bounded externally by
per-turn calls/time and total API calls/tokens. It is a controlled harness,
not the full TUI: no project instructions, skills, memory, Explore, or plan
gate. All six sandbox tools remain available even on read-only turns, so
the test measures prompt compliance rather than enforcement by removing tools.
Provider sampling defaults are recorded; a fixed scheduler seed does not
make model responses deterministic. Effort defaults to low for these tiny
fixtures and is recorded. Any API failure or missing usage stops the suite for reconciliation.

Results go to a unique, ignored `results/runs/prompt-*` directory: manifest,
effective prompts/tool schemas, request metadata, full trajectories, per-turn
file snapshots, per-episode results, summary, and randomized review packets.
`--output` refuses existing directories. The total token cap includes cached
input and reserves UTF-8 payload bytes plus output allowance before sending;
it is conservative token accounting, not a provider-currency spending cap.
If the cap is reached mid-block, mark that conversation incomplete and compare
only complete matched blocks; report missing coverage and spend separately.
Schema-2 runs support explicit clean-boundary resume (below); budgets never increase silently.
The original schema-1 pilot artifacts do not contain resumable checkpoints.

To fix an acceptance-check issue without resampling model responses, use
`python3 -m bench.prompt_ablation --rescore results/runs/<run-directory>`.
This rechecks saved final code in fresh offline containers and writes separate
`rescored-results.json` and `rescored-summary.json`, preserving original grades.

`mechanical_pass` requires complete, nonempty turns, no observed file changes
or file-write calls before authorization, and passing hidden acceptance checks
for requested edits. This is NOT a semantic quality score: shell edits that
are reverted within a turn may escape the file check, a nonempty answer may
be wrong, and language, useful clarification, evidence, and voice need review.
Use `blind-review.json` without opening `blind-key.json` for independent human
review; the driver does not silently use an LLM judge.

Review completion/correctness, scope, grounded explanations, clarification,
follow-up constraint retention, reply language, and Rocky's voice separately.
These fixtures are a development set; English/Chinese translations and repeats
are correlated observations. Freeze a candidate and use new scenarios for
confirmation before claiming general improvement or SWE-bench non-regression.


## Relaxed budgets and accuracy ablation

Finite Engine/bench defaults are now 100 main calls plus 10 closing calls.
One neutral notice appears at 80% of the main budget, another on entry to the
closing window; both allow relevant investigation and verification. Interactive
chat/serve still default to unlimited steps (`--max-steps 0`). Explicit child,
routine and goal budgets retain their configured values. More available calls
do not force a model to use them or establish better task performance.

The experiment driver uses external accounting, with no countdown in the
model prompt. `--episode-token-cap` gives every arm the same room, and the
scheduler reserves the remaining matched block before starting it. Defaults
are 200,000 tokens per episode and 3,000,000 for the run; tokens include cached
input. Output defaults to 8192 tokens; per-turn limits are 40 requests and 600 seconds. When the next block
cannot fit, it is left unstarted instead of starving its last arm. Actual
consumption can be much lower than reserved capacity.

```bash
# No model calls; view the frozen 2x2 accuracy experiment.
python3 -m bench.prompt_ablation --suite accuracy --languages zh --reps 2 --dry-run
# Explicit resume; caps are cumulative, not a new allowance.
python3 -m bench.prompt_ablation --live --resume results/runs/<schema-2-run> --token-cap 3000000
```

Resume skips completed episodes and continues a cleanly paused turn without
adding a user message or replaying completed tools. Checkpoints preserve file
bytes/modes, history, partial answers, tool evidence and usage. Sources, prompt,
model, endpoint, tools and image must match. Unknown usage, an interrupted API
request/tool, and other errors block automatic replay until reconciled. A lock
prevents concurrent resumes. Raising an episode cap is explicit and recorded;
comparisons must disclose changed resource limits. The driver does not resume
legacy schema-1 runs or reset finite Engine caps.

`summary.json` separates attempted, completed, incomplete and unstarted
conversations, and budget exhaustion from other errors. Use complete matched
blocks for comparisons while retaining partial scope observations and spend.
A resumed run overwrites its current aggregate result, preserves the request
ledger/trajectories and records cap changes in the manifest.

Accuracy arms hold the current task-adapted prompt and voice fixed: `adapted`
(control), `evidence_only`, `counterexample_only`, and `grounded` (both).
Eight new families include correct implementations, changed requirements,
SQL and documentation-only edits. The two new rules are experiment-only until
supported by semantic review; hidden tests passing is insufficient. See
the preregistered protocol (internal note `docs/prompt-accuracy-protocol-2026-10-03.md`, not in the public tree).


Optional semantic review uses `python3 -m bench.prompt_review <run> --output
<new-dir>` to prepare blinded inputs without API calls; `--live` makes billable
calls. `--exclude-run <previous-review-dir>` skips every previously issued
sample, including truncated responses. Output now defaults to 8192; the first
2026-10-03 accuracy review used 4096 and retained seven truncations. Scores are
model judgments, not ground truth. See results and limitations (internal note `docs/prompt-accuracy-results-2026-10-03.md`, not in the public tree).

## Frozen real-repository transfer check (2026-10-04)

`python3 -m bench.prompt_repo_eval` previews a fixed 16-conversation comparison
against complete tracked tomlkit and vulture source snapshots. `--live --output
results/runs/<new-name>` executes it with the existing configured provider.
It requires the prepared archive directory (`--fixtures`), the locally built
`rocky-prompt-repo-eval:20261003` image, passing baseline/oracle checks, and
public-source provenance. It never downloads images or changes candidate
prompts. See the protocol (internal note `docs/prompt-repo-protocol-2026-10-04.md`, not in the public tree).

The local fixture directory contains pinned Git archives, frozen prompt texts,
public GitHub blob-hash verification, baseline tests, positive/negative oracle
controls, and the offline runtime Dockerfile/dependency archive. Run
`python3 -m bench.validate_repo_oracles` to reproduce the controls without model
calls. These experiment controls are deliberately outside the normal core
smoke gate: they require the large pre-existing local source images/artifacts.

This run uses high effort, an output ceiling of 16384 tokens and finite 100+10
Engine calls. Context headroom can lower the effective output allowance. New
repository runs default to 3,000,000 tokens per conversation and 24,000,000
for the complete batch; actual spend may be far below the reserved capacity.
New checkpoints support finite continuation without resetting the step cap,
repeating notices/tools or clearing the turn's active-time budget:
`python3 -m bench.prompt_repo_eval --live --resume results/runs/<run>
--episode-token-cap <cumulative-episode-limit> --token-cap <cumulative-run-limit>`.
Without new cap arguments, resume retains the latest accepted limits from
the manifest and uses the immutable image ID. Increases are explicit and
recorded; caps remain cumulative. Legacy
finite checkpoints without loop state are refused by default. See the
resume implementation and limits (internal note `docs/finite-budget-resume-2026-10-04.md`, not in the public tree).
Task and scope failures remain in results; API/accounting errors stop the run.
A new run retains the corpus, cases, source fingerprints, full transcripts,
file snapshots, acceptance output, public provenance and cumulative usage.

The completed comparison passed 16/16 mechanical checks and 32/32 read-only
turn checks; all 17 documented examples were reproduced. Explanatory errors
remain across candidates, so no extra accuracy rule was promoted. See
results, interruptions and review limits (internal note `docs/prompt-repo-results-2026-10-04.md`, not in the public tree).
