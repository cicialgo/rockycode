"""Vision routes: how rocky understands an image when the ACTIVE model can't.

A model with native vision just gets pixels (engine/images.py inflates them at
the API boundary) and decides for ITSELF what matters in them — this module is
never consulted. It exists for the text-only case: such a model has three ways
to "see", all producing a DESCRIPTION that is stored on
the image part itself ({"type": "image_path", …, "description": …}) so it
lands in history + trajectory once, survives resume, and is never re-billed:

  provider — one-shot describe call to a keyed VISION endpoint from the same
             registry (deepseek-flash on the home key by default; kimi /
             minimax / glm / qwen / mimo / stepfun when keyed). In-process.
  cli      — the user's own command: stdout becomes the description. A bare
             known tool name ("mmx") expands via KNOWN_CLIS to its real
             describe invocation; a full template with {path} / {question}
             also covers delegation-by-subagent for free — `rockycode exec
             --image {path} …` or any agentic CLI.

The same two backends serve the model-facing `view_image` tool (engine/tools.py),
so the generate-with-PIL-then-review-like-a-human loop works mid-task: the
model writes chart.png, calls view_image, reads what a vision model saw.

One config key says which (`image`, read by image_setting): auto · ask · off ·
an endpoint spec that pins the describer (kimi:kimi-k3) · a command (mmx, or a
template with {path}). The paste-time picker for `ask` lives in the TUI.
"""
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rockycode.engine import providers as P
from rockycode.engine.images import data_url
from rockycode.onboarding import tool_env

PROVIDER_TIMEOUT = 120.0
CLI_TIMEOUT = 240.0        # an image CLI may itself be an agent (mmx) — allow real work
MAX_DESCRIPTION_CHARS = 8_000

# Vision CLIs rocky knows by name: `/config image mmx` is the whole setup —
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


@dataclass(frozen=True)
class ImageSetting:
    """What the one `image` config value means."""
    route: str              # auto | ask | off | provider | cli
    preferred: str = ""     # provider: the pinned endpoint spec
    template: str = ""      # cli: the command (a known name or a template)


def image_setting(value: object) -> ImageSetting:
    """Classify the `image` config value. The three words first; a command is
    a known tool name, anything with a space or a {path}/{question} slot, or
    an executable path; everything else is an endpoint spec (kimi,
    kimi:kimi-k3, openrouter:vendor/model)."""
    text = value.strip() if isinstance(value, str) else ""
    low = text.lower()
    if not text or low == "auto":
        return ImageSetting("auto")
    if low in ("ask", "off"):
        return ImageSetting(low)
    if text in KNOWN_CLIS or " " in text or "{" in text or text.startswith(("/", "./", "~")):
        return ImageSetting("cli", template=text)
    return ImageSetting("provider", preferred=text)


def sidecar_choice(preferred: str = "", *, strict: bool = False) -> Optional[P.Choice]:
    """The vision (endpoint, model) a sidecar describe call would use.

    `preferred` (a pinned `image` endpoint) uses the same spec shape as /model —
    an endpoint id (`kimi`) or `endpoint:model` (`kimi:kimi-k3`, substring
    ok) — so the user pins not just whose key but exactly WHICH model
    describes images. Unset falls back to the registry's keyed `sidecar`
    model (deepseek-flash rides the home key, so it is the zero-setup default
    describer), then the first other KEYED vision endpoint. An unmatched pin
    falls back the same way unless `strict`, where it is None: a pin the user
    named must never silently become another provider. None → no provider
    route."""
    picks = [c for c in P.choices() if c.vision and c.configured]
    picks.sort(key=lambda c: 0 if "sidecar" in c.spec.roles else 1)
    if preferred:
        eid, model = P.split_spec(preferred)  # first `:`/space only — ollama
        model = model.lower()                 # tags keep their inner colon
        for c in picks:
            if c.prov_id == eid and (not model or model in c.model.lower()):
                return c
        if strict:
            return None
    return picks[0] if picks else None


async def describe_provider(
    path: Path, question: str = "", *, preferred: str = "",
) -> tuple[str, str]:
    """One-shot describe via a keyed vision endpoint. Returns (description,
    source label like 'deepseek:deepseek-flash')."""
    choice = sidecar_choice(preferred, strict=bool(preferred))
    if choice is None:
        if preferred:
            raise VisionError(
                f"image = {preferred}: no keyed vision endpoint matches — key it, "
                "/config image auto, or a command: /config image mmx"
            )
        raise VisionError(
            "no keyed vision endpoint — set ROCKYCODE_API_KEY (deepseek-flash "
            "sees) or a ROCKYCODE_<KIMI|MINIMAX|GLM|QWEN|MIMO|STEPFUN>_API_KEY, "
            "or name a vision CLI: /config image mmx"
        )
    from rockycode.engine import route as route_mod  # deferred: this module must import cheap

    prompt = DESCRIBE_PROMPT
    if question.strip():
        prompt += f"\n\nThe agent specifically wants to know: {question.strip()}"
    try:
        to = route_mod.bind(choice.provider, choice.endpoint, choice.model, gateway_ok=False)
    except route_mod.RouteError as e:
        raise VisionError(f"{choice.id}: {e}") from e
    client = route_mod.open_client(to, max_retries=2, timeout=PROVIDER_TIMEOUT)
    # Thinking OFF (or the lowest tier where it can't be off): max_tokens
    # includes CoT on thinking models, so an enabled default would eat the
    # 1024 budget before the description starts.
    from rockycode.engine.effort import build_extra_body
    extra = build_extra_body(False, "high", choice.provider.reasoning,
                             efforts=choice.provider.efforts_for(choice.model),
                             thinking_off=choice.provider.thinking_off_for(choice.model))
    try:
        resp = await client.chat.completions.create(
            model=choice.provider.api_model(choice.model),
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
        raise VisionError(f"image command does not parse: {e}") from e
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
    """Describe via the user's image CLI (config `image = <command>`): stdout
    becomes the description. See _cli_argv for how the command line is built.
    The command gets tool_env(), like every process rocky starts: the user's
    environment without rocky's keys. A tool with its own login (mmx) keeps it;
    a nested `rockycode` loads the store itself."""
    if not template.strip():
        raise VisionError("no image command configured — /config image mmx "
                          "(or a full command template with {path})")
    argv = _cli_argv(template, path, question)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=tool_env())
        out, errout = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
    except FileNotFoundError:
        raise VisionError(f"image command not found: {argv[0]}") from None
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise VisionError(f"image command timed out after {int(CLI_TIMEOUT)}s: {argv[0]}") from None
    if proc.returncode != 0:
        detail = (errout or out).decode(errors="replace").strip()[-400:]
        raise VisionError(f"image command exited {proc.returncode}: {detail or argv[0]}")
    text = out.decode(errors="replace").strip()
    if not text:
        raise VisionError(f"image command produced no output: {argv[0]}")
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
