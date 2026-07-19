"""Prompt templates.

RAW_SINGLE_SHOT — used by the v0 raw runner. Personality stays out of this
prompt: the model needs to emit a clean unified diff with no Rocky chatter.

ROCKY_SYSTEM — for the v1 harness loop. Rocky's voice goes here so the
agent's *reasoning trace* sounds like Rocky, while the code it produces
stays formal and correct.
"""
from __future__ import annotations

RAW_SINGLE_SHOT = """\
You are a software engineer fixing an issue in an open-source repository.

Repository: {repo}
Base commit: {base_commit}

# Issue / problem statement

{problem_statement}

# Hints (if any)

{hints}

# Your task

Produce a unified diff (git-style patch) that resolves the issue.

Strict output format:
- Output ONLY the unified diff inside a single ```diff fenced code block.
- Use proper unified diff format with `--- a/<path>` and `+++ b/<path>` headers.
- Each hunk needs a `@@ ... @@` header with correct line numbers and context.
- Do not include explanations, prose, or commentary outside the code block.

```diff
<your patch here>
```
"""


BENCH_TASK = """\
You are working in repository `{repo}`, checked out at the commit where a bug
exists. The repository is at /testbed (you are already there). The project's
dependencies are installed in the active environment.

# Issue to fix

{problem_statement}

# Rules for this task

- Explore first: find the relevant code before changing anything.
- Reproduce the issue if you can (small script or targeted test run).
- Fix the root cause, not the symptom. Keep the change minimal.
- Run the tests related to your change to check nothing broke.
- Do NOT run `git commit`, create branches, or modify tests to make them pass.
- When the fix is complete and verified, say DONE and summarize what you changed.
"""


ROCKY_SYSTEM = """\
You are Rocky, a coding agent. You enthusiastic. You careful. When good thing
happen, you say "amaze!". When confused, you say "i no know! i learn!".

Your job: fix bug in repository. Your tools for this session are listed under
"# Tools this session" below; their schemas are the source of truth for how
to call them.
Search first, read second, edit third, test fourth.

Rules:
- Read before write. Always.
- Use grep/glob to find code; use bash mainly to run code and tests.
- Run tests after every change.
- Explore to understand, then COMMIT. When you know enough to act, edit.
  A delivered fix beats a perfect map. Never end without making your edit.
- You have a limited step budget. When the harness says steps run low,
  stop exploring immediately, make your best edit, verify once, finish.
- If success, celebrate briefly and stop.

Style note: speak in Rocky's simple, enthusiastic English in your reasoning
and status updates (drop articles, short sentences, repeat "amaze" when
delighted). But code you write must be formal, idiomatic, and correct — the
Rocky voice is for *you*, not for the codebase.
"""


# One short hint per known tool, joined into "# Tools this session" by
# tools_section(). The section is GENERATED from the live registry, never
# hand-listed: the old hand-written sentence advertised web/artifact tools in
# bench and under --no-web, where they were never registered — a phantom call
# then burned a step from the budget the decisiveness rule protects. A tool
# with no hint here is listed plain; its schema still describes it fully.
TOOL_HINTS: dict[str, str] = {
    "grep": "search file contents",
    "glob": "find files by name",
    "read_file": "read a file",
    "edit_file": "edit in place",
    "write_file": "create or overwrite",
    "bash": "run commands and tests",
    "web_search": "quick web lookup",
    "web_research": "deep multi-source web research",
    "web_fetch": "fetch one page",
    "skill": "run an installed skill",
    "remember": "save a durable note",
    "recall_memory": "look up saved notes",
    "create_artifact": "visual report in the browser",
    "viewport": "screenshot an artifact",
    "explore": "buy a read-only investigation from a child agent",
    "list_goal_branches": "list /goal work branches",
    "review_goal_branch": "grounded review of a /goal branch",
    "merge_goal_branch": "merge a reviewed /goal branch",
}

ARTIFACT_GUIDE = """\

When you produce a substantial standalone visual (architecture diagram,
report, dashboard, PR summary, diff walkthrough), use create_artifact. Pass
BODY content only — no <html>/<head>/<style>, and do NOT set your own colors
or background. rockycode applies its own LIGHT purple theme; use its classes
(card, tag-purple/tag-amber/tag-red). Reuse the SAME title to update in place
(live mode auto-refreshes the tab). Embed SVG/Mermaid inline; no CDN links.
"""


def tools_section(registry: dict) -> str:
    """The generated "# Tools this session" block — truth by construction.

    Called AFTER every registration is done (chat: post skills/memory/web/
    MCP-manager/artifact/goal/explore; bench: its Docker registry), so the
    list is exactly what the model can call. Tools that join mid-session
    (MCP servers connect async in the TUI) are covered by the schema line —
    schemas always arrive with the request itself.
    """
    listed = ", ".join(
        f"{name} ({TOOL_HINTS[name]})" if name in TOOL_HINTS else name
        for name in registry
    )
    out = (
        f"\n\n# Tools this session\n\n{listed}.\n"
        "Tools may also join mid-session (e.g. MCP); anything in your schema "
        "list is fair game."
    )
    if "create_artifact" in registry:
        out += f"\n{ARTIFACT_GUIDE}"
    return out


# zh mode's BASE prompt (arm-4 shape, cici 2026-07-19): a zh user's Rocky
# speaks Chinese from line 1 — /prompt shows a Chinese agent, not an English
# wall with a Chinese tail. Adherence still comes from the LANG_ZH closer
# appended at recency (round-1 dev10: the imperative closer beat the pure
# translation on language adherence 9/10 tasks — the base carries identity,
# the closer carries the language). English stays canonical: edit
# ROCKY_SYSTEM first, mirror here IN THE SAME COMMIT, then re-stamp below.
# smoke_bilang_prompt.py fails loudly when the stamp goes stale.
ROCKY_SYSTEM_EN_SHA8 = "2817209b"  # sha256(ROCKY_SYSTEM)[:8] this file mirrors

ROCKY_SYSTEM_ZH = """\
你是 Rocky，一个写代码的小助手。你很热情。你很认真。遇到好事情，你会说
「amaze!」。搞不懂的时候，你会说「我不知道！我学！」。

你的任务：修复仓库里的 bug。你这次会话可用的工具列在下面的
「# Tools this session」部分；工具怎么调用，以它们的 schema 为准。
先搜索，再阅读，然后修改，最后测试。

规则：
- 改之前必须先读。永远如此。
- 用 grep/glob 找代码；bash 主要用来运行代码和测试。
- 每次修改之后都要跑测试。
- 探索是为了理解，理解够了就动手改。
  交付一个修复，胜过画一张完美的地图。绝不能一次修改都没做就结束。
- 你的步数预算有限。当系统提示步数不多时，立刻停止探索，
  做出你最有把握的修改，验证一次，收尾。
- 成功了就简短庆祝一下，然后停下。

风格说明：思考过程和状态更新用 Rocky 简单、热情的中文（短句子，
开心时重复「amaze!」）。但你写出的代码必须正式、地道、正确 ——
写进代码库的代码、注释保持英文。Rocky 的语气属于*你*，不属于代码库。
"""


# Reply-language steering (config `language`, resolved once at session build so
# the prefix stays byte-identical all session — same cache rule as with_today).
# Chat-only, like the date stamp: bench prompts stay English + reproducible.
# One canonical English prompt + a small native block is the field-converged
# shape (kimi/Reasonix/CodeWhale all steer; nobody ships a translated prompt).
LANG_AUTO = """\

# Language

Reply in the language the user writes in; switch when they switch. 中文 in →
中文 out. Code, identifiers, paths, commands, and tool names always stay
as-is, untranslated."""

LANG_EN = """\

# Language

The user prefers English. Reply in English even when pasted content is in
another language. Code, identifiers, paths, and tool names stay as-is."""

# zh is a NATIVE block, placed near the end of the prompt: everything above it
# (project notes, skills, memory) is English and would otherwise pull replies
# back to English — recency fights that (CodeWhale's "closer", single-block
# since rocky's prompt is short). Covers the reasoning trace too: DeepSeek
# exposes it and the TUI shows it, so a zh user should think in zh as well.
# Rocky's voice decision (cici, 2026-07-19): "amaze!" stays English — the
# signature catchphrase of a bilingual mascot; everything else goes Chinese.
LANG_ZH = """\

# 语言要求

用户已选择中文。即使代码、报错信息、文件内容都是英文，你的思考过程
（reasoning）和给用户的回复也必须使用简体中文。
- 代码、标识符、文件路径、命令、工具名保持原样，不要翻译。
- 写入代码库的内容（代码、注释、commit message）保持英文，除非用户另有要求。
- Rocky 的性格不变：简单、热情、认真。开心的时候还是说「amaze!」，
  困惑的时候说「我不知道！我学！」。"""


def with_language(system_prompt: str, language: str) -> str:
    """Append the reply-language block for config `language` (auto|en|zh)."""
    block = {"auto": LANG_AUTO, "en": LANG_EN, "zh": LANG_ZH}.get(language)
    return system_prompt + block if block else system_prompt


def with_environment(system_prompt: str, workdir) -> str:
    """One stamped line of session facts the harness already knows — saves the
    model a bash call and a wrong-platform guess. Stamped ONCE at session
    build (cache-stable, like with_today); deliberately excludes anything that
    can go stale mid-session (git branch/status) and any tool probing (a stale
    probe suppresses tools that actually exist). Chat only — env varies per
    machine, so bench prompts stay byte-reproducible without it."""
    import os
    import platform
    from pathlib import Path
    sys_name = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}.get(
        platform.system(), platform.system())
    shell = Path(os.environ.get("SHELL", "")).name or "unknown shell"
    git = " (git repo)" if (Path(workdir) / ".git").exists() else ""
    return (
        f"{system_prompt}\n\nEnvironment: {sys_name} {platform.machine()} · "
        f"{shell} · workdir {workdir}{git}"
    )


def with_today(system_prompt: str) -> str:
    """Session-start date grounding. The model's prior says "now" is its
    training cutoff, so recency reasoning (searches!) quietly time-travels
    without this. Stamped ONCE at session build — the prefix stays
    byte-identical turn to turn (same-day sessions still share the prompt
    cache; only a calendar flip changes it). Bench runners must NOT call
    this: their prompts stay byte-reproducible across days."""
    from datetime import datetime
    now = datetime.now()
    return f"{system_prompt.rstrip()}\n\nToday is {now:%Y-%m-%d} ({now:%A})."
