"""Vision routes: how rocky understands an image when the ACTIVE model can't.

A model with native vision just gets pixels (engine/images.py inflates them at
the API boundary) and decides for ITSELF what matters in them — this module is
never consulted. It exists for the text-only case: such a model has three ways
to "see", all producing a DESCRIPTION that is stored on
the image part itself ({"type": "image_path", …, "description": …}) so it
lands in history + trajectory once, survives resume, and is never re-billed:

  provider — one-shot describe call to a keyed VISION endpoint from the same
             registry (minimax / kimi / stepfun). In-process, another API key.
  cli      — the user's own command (`image_cli` config): stdout becomes the
             description. A bare known tool name ("mmx") expands via
             KNOWN_CLIS to its real describe invocation; a full template with
             {path} / {question} also covers delegation-by-subagent for free —
             `rockycode exec --image {path} …` or any agentic CLI.
  (paste-time picking between these lives in the TUI: config `image_route`.)

The same two backends serve the model-facing `view_image` tool (engine/tools.py),
so the generate-with-PIL-then-review-like-a-human loop works mid-task: the
model writes chart.png, calls view_image, reads what a vision model saw.

Config (/config): image_route ask|provider|cli|off · image_provider <endpoint
id, "" = first keyed vision endpoint> · image_cli mmx (or a full template).
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Optional

from rockycode.engine import providers as P
from rockycode.engine.images import data_url

PROVIDER_TIMEOUT = 120.0
CLI_TIMEOUT = 240.0        # an image CLI may itself be an agent (mmx) — allow real work
MAX_DESCRIPTION_CHARS = 8_000

# Vision CLIs rocky knows by name: `/config image_cli mmx` is the whole setup —
# the user should never have to learn a tool's argument shape. Each recipe is
# grounded against the tool's own --help (mmx: `mmx vision describe
# --image <path> [--prompt <text>]`). Anything not listed here still works as
# a full template with {path} / optional {question}.
KNOWN_CLIS = {
    "mmx": "mmx vision describe --image {path} --prompt {question}",
}

DESCRIBE_PROMPT = (
    "You are the eyes for a text-only coding agent working in a terminal. "
    "Describe this image precisely and usefully: all readable text verbatim "
    "(code, error messages, labels, log lines), UI structure/layout, colors "
    "only when they carry meaning, and anything that looks broken or "
    "anomalous. Be dense; facts over prose."
)


class VisionError(Exception):
    """A route failed (no key, CLI missing, API error). Message is user-facing."""


def sidecar_choice(preferred: str = "") -> Optional[P.Choice]:
    """The vision (endpoint, model) a sidecar describe call would use.

    `preferred` (config image_provider) uses the same spec shape as /model —
    an endpoint id (`minimax-cn`) or `endpoint:model` (`minimax-cn:minimax-m3`,
    substring ok) — so the user pins not just whose key but exactly WHICH
    model describes images. Unset/unmatched falls back to the first KEYED
    vision endpoint (what the picker row then shows) — deepseek rides the
    default key, so since flash-vision-exp its vision model is the zero-setup
    default describer. None → no provider route."""
    picks = [c for c in P.choices() if c.vision and c.configured]
    if preferred:
        eid, _, model = preferred.replace(":", " ").partition(" ")
        eid, model = eid.strip(), model.strip().lower()
        for c in picks:
            if c.prov_id == eid and (not model or model in c.model.lower()):
                return c
    return picks[0] if picks else None


async def describe_provider(
    path: Path, question: str = "", *, preferred: str = "",
) -> tuple[str, str]:
    """One-shot describe via a keyed vision endpoint. Returns (description,
    source label like 'minimax-cn:minimax-m3')."""
    choice = sidecar_choice(preferred)
    if choice is None:
        raise VisionError(
            "no keyed vision endpoint — set a ROCKYCODE_<KIMI|MINIMAX|STEPFUN>"
            "…_API_KEY (or config image_provider), or configure a vision CLI: "
            "/config image_cli mmx"
        )
    from openai import AsyncOpenAI  # deferred: this module must import cheap

    prompt = DESCRIBE_PROMPT
    if question.strip():
        prompt += f"\n\nThe agent specifically wants to know: {question.strip()}"
    client = AsyncOpenAI(api_key=choice.endpoint.key(), base_url=choice.endpoint.base_url,
                         max_retries=2, timeout=PROVIDER_TIMEOUT)
    # Thinking OFF for a deepseek-policy describer: max_tokens includes CoT on
    # DeepSeek, so an enabled default would eat the 1024 budget before the
    # description starts.
    from rockycode.engine.effort import build_extra_body
    extra = build_extra_body(False, "high", choice.provider.reasoning)
    try:
        resp = await client.chat.completions.create(
            model=choice.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                # base64 data URL: the one image form EVERY registry vision
                # provider accepts (kimi rejects public https URLs).
                {"type": "image_url", "image_url": {"url": data_url(path)}},
            ]}],
            max_tokens=1024,
            stream=False,
            extra_body=extra,
        )
    except Exception as e:  # noqa: BLE001 — surfaced as a route failure
        raise VisionError(f"{choice.id}: {type(e).__name__}: {e}") from e
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise VisionError(f"{choice.id} returned an empty description")
    return text[:MAX_DESCRIPTION_CHARS], choice.id


def _cli_argv(template: str, path: Path, question: str) -> list[str]:
    """The argv a cli-route describe runs. A bare known tool name expands via
    KNOWN_CLIS; {path} is replaced with the image path, {question} with the
    focus question (DESCRIBE_PROMPT when there is none, so a recipe's --prompt
    flag never receives an empty string). Built WITHOUT a shell — the template
    is shlex-split and substitution happens per-argument, so a filename can't
    inject commands."""
    try:
        argv = shlex.split(template)
    except ValueError as e:
        raise VisionError(f"image_cli template does not parse: {e}") from e
    if len(argv) == 1 and argv[0] in KNOWN_CLIS:
        argv = shlex.split(KNOWN_CLIS[argv[0]])
    q = question.strip() or DESCRIBE_PROMPT
    argv = [a.replace("{path}", str(path)).replace("{question}", q) for a in argv]
    if not any(str(path) in a for a in argv):
        argv.append(str(path))  # template without {path} → path as last arg
    return argv


async def describe_cli(
    path: Path, question: str = "", *, template: str,
) -> tuple[str, str]:
    """Describe via the user's image CLI (config image_cli): stdout becomes
    the description. See _cli_argv for how the command line is built."""
    if not template.strip():
        raise VisionError("image_cli is not configured — /config image_cli mmx "
                          "(or a full command template with {path})")
    argv = _cli_argv(template, path, question)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, errout = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
    except FileNotFoundError:
        raise VisionError(f"image_cli command not found: {argv[0]}") from None
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise VisionError(f"image_cli timed out after {int(CLI_TIMEOUT)}s: {argv[0]}") from None
    if proc.returncode != 0:
        detail = (errout or out).decode(errors="replace").strip()[-400:]
        raise VisionError(f"image_cli exited {proc.returncode}: {detail or argv[0]}")
    text = out.decode(errors="replace").strip()
    if not text:
        raise VisionError(f"image_cli produced no output: {argv[0]}")
    return text[:MAX_DESCRIPTION_CHARS], argv[0]


async def describe(
    path: Path | str, question: str = "", *,
    route: str = "auto", preferred: str = "", template: str = "",
) -> tuple[str, str]:
    """Route an image to a description. route: 'provider' | 'cli' | 'auto'
    (auto = the user's CLI when configured, else the provider sidecar — the
    CLI is the more deliberate setup, so it wins)."""
    p = Path(path)
    if route == "cli" or (route == "auto" and template.strip()):
        return await describe_cli(p, question, template=template)
    return await describe_provider(p, question, preferred=preferred)
