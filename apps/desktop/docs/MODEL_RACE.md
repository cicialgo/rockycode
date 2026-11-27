# Model Race / 田忌赛马 (preview)

A desktop-first toy: one task, five distinct configured LLMs, five independent Rocky Engine runs. No second agent loop. It is a single-case comparison, not an authoritative benchmark or global leaderboard. TUI and iOS entry points are not included.

## Play

1. Configure five different model identities in RockyCode's existing provider configuration. Open **Model Race / 田忌赛马** in the desktop sidebar. Only exact configured endpoint/model choices are offered; there is no fallback to the default provider.
2. Enter one task. Optionally select a small reviewed task-input folder. The default is prompt-only. All five lanes receive the same task and immutable input snapshot, then operate in separate private directories. Inputs may be sent to the selected model providers when read; review them first.
3. Pick equal model-call, output-token-per-call, and wall-clock limits. Start explicitly. Five concurrent model runs can incur provider charges. Call/token/time limits are not a hard dollar cap.
4. Watch anonymous A–E lanes, output, tool activity and result files. Review available token counts, elapsed wall time and estimated cost. Missing provider usage or pricing is shown as unknown, never fabricated zero. Cancellation retains partial results; failures affect individual lanes. Interrupted races are restored without auto-replay. Starting a race brings the lanes into view; above them a finish board shows five flags waiting at the start. When the race is over, the board replays it in about two seconds: each flag reaches the line in the order its lane really finished, and a lane that failed gets no place. The order is by time only and says nothing about quality; model names appear there only after the reveal.
5. Save independent human scores (0–10 or abstain). Run a separately selected model judge only when requested. It has no tools, sees randomized anonymous candidates with a fixed rubric, and must return strictly validated scores. It sees bounded excerpts; truncation is flagged and it may abstain. Human and model scores never overwrite one another. Model identities/judge scores are hidden until human scores are saved or explicitly revealed.
6. Generate a full local JSON preview, review it, then explicitly confirm and choose a save location. The saved digest must still match the preview. Nothing is uploaded automatically or manually by this version. No website destination or ranking backend is configured.

Local model choices exclude models whose native metadata lacks either completion
or tool calling, including embedding-only models. Ollama models are offered only
after their capabilities can be read; metadata failures are retried after five
seconds. Successful metadata is cached for 30 seconds. Other compatible local
servers without Ollama's native metadata retain their declared tool support.
This same allowlist applies to contestants, judges and direct backend requests.

## Safety and scope

- Same upstream `build_runtime` and `Engine.run_turn`, with an explicit isolated profile that bypasses ambient project instructions, global project/trajectory records, modes and user memory. Normal chat defaults remain unchanged.
- Only `read_file`, `write_file` and `list_files` are available, confined with directory-relative no-follow filesystem operations. No shell, code execution, browser, network tools, MCP, skills, self-configuration, extra agents or reflection calls. Model provider calls are the intentional network activity. This is a restricted capability profile, **not an OS sandbox**.
- Snapshot limits: 128 UTF-8 regular files, 256 KiB/file, 2 MiB total, 1,024 visited entries, depth 12. Hidden files, common secret names and caches are excluded. Symlinks, hardlinks, special files, binary data and exceeded limits reject the snapshot. Exclusion names are a defense in depth, not a secret detector.
- Original input files are never written. The source is read once before any lane starts; all five copies use the same frozen bytes and hash. Generated files stay local to the race; the original project is not automatically patched.
- Files-only confinement currently requires POSIX `dir_fd` and `O_NOFOLLOW` support. Unsupported platforms fail closed. Windows race execution is not supported by this preview; no Windows/macOS live validation is claimed here.
- History loads in pages of 20 summaries, with full results fetched only when selected. Result-file evidence is capped at 100,000 characters per lane; truncation is explicit.
- Judge output is untrusted. It cannot call tools; unknown IDs, duplicate IDs, missing candidates, non-finite/out-of-range scores and invalid JSON are rejected. Blinding reduces identity/position bias but cannot prevent answers from identifying themselves, nor make an LLM judgment authoritative.
- Exports allowlist task, answers, model identifiers, limits, input hash, metrics and separate scores. They exclude generated files, source locations, tool arguments, raw errors, session history and credentials. Text redaction is best effort; the user must inspect for private information before sharing elsewhere. Source-file content can still appear in model answers.
- Race records and working copies remain in the app's local data directory. This preview has no automatic deletion/retention policy and no submission service, website deployment, account changes or global ranking.

## Offline validation

No live paid models are needed:

```sh
# Existing core CI discovers smoke_desktop_race.py automatically.
PYTHONPATH="$PWD:$PWD/apps/desktop/runtime" .venv/bin/python3 tests/smoke_desktop_race.py
PYTHONPATH="$PWD:$PWD/apps/desktop/runtime" .venv/bin/python3 -m unittest discover -s apps/desktop/runtime/tests -v
cd apps/desktop
npm run check
node --import tsx --test tests/*.test.ts
npm run test:e2e:race
```

The race smoke test fails on skipped core tests. It includes actual upstream Engine file/tool turns against five fake transports, snapshot/confinement attacks, missing provider fail-closed behavior, strict call accounting, cancellation, partial failure, restart recovery, judge schema and reviewed local export with network calls forbidden.

`e2e-race.mjs` uses the real Electron renderer with the explicit offline host fixture. It checks navigation/drafts, five distinct lanes, independent scoring, judge reveal, stale export digest, local-save cancellation, partial failure, cancellation and interrupted restart. It does not validate real model quality or costs. Run it only after preparing the pinned Electron binary under the existing supply-chain process.

## Live validation (paid)

Five configured models, one judge call and a local export, through the real Electron renderer, IPC, Python host and providers:

```sh
cd apps/desktop
npm run build
npm run test:e2e:race:live -- --live
```

It uses the first five distinct provider/model pairs the app offers (`ROCKYCODE_RACE_MODELS=id1,…,id5` and `ROCKYCODE_RACE_JUDGE=id` override), 4 model calls and 512 output tokens per lane, a 120-second lane limit, and writes `artifacts/race-live.json` plus screenshots. Provider charges apply. The state directory is temporary, so nothing lands in the normal app data.
