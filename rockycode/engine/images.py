"""Image input: attach pictures to a user turn, provider-safely.

The OpenAI content-array shape is the ONLY wire format rocky speaks — every
vision provider in the registry (stepfun / minimax-m3 / kimi-k3) accepts
`{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}`
through the stock OpenAI SDK. Base64 data URLs are the one form ALL of them
take: Kimi explicitly rejects public https image URLs, so remote URLs are
never sent. No per-provider adapter, matching providers.py's anti-glue rule.

In-memory history and the trajectory JSONL carry a small INTERNAL part
instead of the base64 blob:

    {"type": "image_path", "path": "/abs/…/shot.png", "media_type": "image/png"}

and the blob is inflated only at the API boundary (api_view, called at every
chat.completions.create site). Why: the trajectory mirrors history verbatim,
and a single screenshot is ~1–10 MB of base64 — inlined, one paste would
dwarf every other line of the training log; as a path, resume replays it and
the log stays greppable. The flip side is a hard rule: an image_path part
must NEVER reach the wire — api_view is the only exit and it always converts.

When the active model has no vision (deepseek home model, or a /model switch
away from one that did), api_view collapses list content to a plain string —
DeepSeek is not documented to take content arrays, and a text placeholder
tells the model an image existed without pretending it saw it.
"""
from __future__ import annotations

import base64
import shlex
from pathlib import Path
from typing import Iterable, Union

from rockycode.engine.trajectory import trajectory_dir

# Formats every vision provider in the registry accepts (JPEG/PNG/GIF/WebP is
# the intersection of stepfun / minimax / kimi; SVG et al. are rejected).
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
IMAGE_EXTS = frozenset(MEDIA_TYPES)

Content = Union[str, list]


def assets_dir(session_id: str) -> Path:
    """Where pasted clipboard images land: sibling of the session's JSONL in
    the global trajectory store, keyed by session id — so a trajectory and the
    pixels it references travel together (and dream/RL export can find them)."""
    d = trajectory_dir() / "assets" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def media_type(path: Union[str, Path]) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "image/png")


def image_part(path: Union[str, Path]) -> dict:
    """The internal history/trajectory form — a reference, never the bytes."""
    p = Path(path).expanduser()
    return {"type": "image_path", "path": str(p), "media_type": media_type(p)}


def make_content(text: str, images: Iterable[dict]) -> Content:
    """User-turn content: the plain string when there are no images (the
    existing wire shape, byte-identical), the OpenAI array form when there are."""
    parts = list(images)
    if not parts:
        return text
    out: list = [{"type": "text", "text": text}] if text else []
    return out + parts


def content_text(content: Content) -> str:
    """The text of a message regardless of shape — for titles, digests, the
    session picker, and any UI that renders history as prose."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def count_images(content: Content) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content
               if isinstance(p, dict) and p.get("type") == "image_path")


def data_url(path: Union[str, Path]) -> str:
    raw = Path(path).read_bytes()
    return f"data:{media_type(path)};base64,{base64.b64encode(raw).decode()}"


def _flatten(parts: list) -> str:
    """List content → one string for a NON-vision endpoint. An image part that
    a vision route already described (engine/vision.py stored `description` on
    it) becomes that description; an undescribed one becomes an honest
    placeholder — the model is told an image exists and was not seen, so it
    can say "switch to a vision model" instead of hallucinating pixels."""
    out = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "image_path":
            name = Path(p.get("path", "?")).name
            desc = (p.get("description") or "").strip()
            if desc:
                out.append(f'[image "{name}" — as seen by '
                           f"{p.get('described_by') or 'a vision model'}]\n{desc}")
            else:
                # Teach the model the enable-menu, not just the limitation —
                # so rocky can GUIDE the user instead of "I can't see images".
                out.append(f'[image "{name}" attached — NOT seen: the current model '
                           "cannot see images and no description was attached. The "
                           "user can enable image understanding with: /config "
                           "image_cli mmx (a local vision CLI), or a keyed vision "
                           "provider (kimi · minimax · stepfun), or /model to "
                           "switch to a vision model.]")
        elif p.get("type") == "text":
            out.append(p.get("text", ""))
    return "\n".join(s for s in out if s)


def _inflate(parts: list) -> list:
    """List content → the OpenAI wire form for a vision endpoint. A missing
    file (moved/deleted between sessions) degrades to a text note — resume
    must not 400 because a screenshot was cleaned up."""
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "image_path":
            try:
                out.append({"type": "image_url",
                            "image_url": {"url": data_url(p["path"])}})
            except OSError:
                name = Path(p.get("path", "?")).name
                out.append({"type": "text",
                            "text": f'[image "{name}" is no longer on disk — '
                                    "ask the user to re-attach it if needed]"})
        else:
            out.append(p)
    return out


def api_view(history: list[dict], *, vision: bool) -> list[dict]:
    """The messages actually sent to chat.completions.create.

    Fast path: a history with no list content (every session until an image
    arrives) is returned AS-IS — same object, zero copies, so the plain-text
    hot path pays nothing. With images: a shallow per-message copy with
    image_path parts inflated (vision) or the whole list collapsed to a
    string (no vision). History itself is never mutated.
    """
    if not any(isinstance(m.get("content"), list) for m in history):
        return history
    out = []
    for m in history:
        c = m.get("content")
        if isinstance(c, list):
            m = {**m, "content": _inflate(c) if vision else _flatten(c)}
        out.append(m)
    return out


def extract_image_paths(text: str) -> list[Path]:
    """Image files referenced in a submitted message — the drag-and-drop path.

    Terminals turn a dropped/Finder-copied file into its (often quoted or
    backslash-escaped) path, so shlex-splitting the message and keeping tokens
    that are existing files with an image extension catches it with no special
    UI. file:// URLs (some terminals paste those) are unwrapped too.
    """
    try:
        tokens = shlex.split(text)
    except ValueError:  # unbalanced quotes — fall back to whitespace
        tokens = text.split()
    found: list[Path] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok.startswith("file://"):
            from urllib.parse import unquote, urlsplit
            tok = unquote(urlsplit(tok).path)
        if Path(tok).suffix.lower() not in IMAGE_EXTS:
            continue
        p = Path(tok).expanduser()
        try:
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                found.append(p)
        except OSError:  # a token the OS refuses to stat (too long, bad bytes)
            continue
    return found
