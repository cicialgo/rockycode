---
name: rocky-delegate
description: Delegate cheap bulk work — full-document translation, long summaries, mechanical refactors — to the rocky MCP tool rocky_exec on deepseek-flash. Use when the task is large and mechanical. Do not use for design, security review, or anything the user did not want sent to DeepSeek. Default profile is read; pass write or full only when the user explicitly asked for edits or commands.
---

# Delegate bulk work to rocky

Use the MCP tool `rocky_exec` (server `rocky`) when the work is large and
mechanical and a cheaper model should do it: translate a whole document,
summarize long files, apply a repetitive rename or formatting change.

Keep the work in this session when it needs judgment: API design, security
review, debugging a failure you do not already understand, or anything the
user told you not to send off-machine.

## How to call

- `task`: the full instruction, including the output shape you want back.
- `model`: leave it `deepseek-flash` unless the user named another rocky model.
- `profile`: leave it `read` unless the user explicitly asked rocky to edit
  files (`write`) or run commands (`full`). Never upgrade the profile because
  the task would be easier that way. `read` cannot write and cannot start a
  shell. `full` needs Docker on the machine and stays sandboxed with no
  network.
- `workdir`: the project directory, when it is not the server's cwd.
- `max_steps`: omit for the default 30. Raise it only for a long mechanical
  run. Exit code 3 means the cap was spent.

## How to read the result

`exit_code` 0 is done. 1 is an error. 2 is blocked on a grant — show
`blocked_on` to the user; do not invent an approval. 3 is the step budget.
Use `summary` as the answer. If your context truncated it, read `output_path`
(a temp file, not a project edit). `evidence` lists files and commands; check
them yourself. `cost_usd` is DeepSeek's price for this call when the model is
in rocky's table. `peak: true` means the peak-hour doubling applied.

## Cost and data

DeepSeek Flash is cheaper than gpt-6.1-sol and not cheaper than gpt-6-luna.
Weekday Beijing peak hours (09:00–12:00 and 14:00–18:00) double DeepSeek's
rates. The task text and any file contents rocky reads are sent to DeepSeek.
Do not delegate secrets or private material the user would not send there.

A long call needs Codex `tool_timeout_sec` well above the 60s default
(1800 is the documented setting). If the tool times out at 60s, the fix is
that Codex setting, not a bigger `max_steps`.
