"""DeepSeek ASR — speech to text on the home key.

POST https://api.deepseek.com/v1/tools/asr (multipart `file` field): one
whole clip in, one transcript out — NO streaming (docs, 2026-11-13), so the
recorder does its own endpointing (engine/voice.EnergyVad) before calling.
Clip caps: 16-bit PCM WAV (8k–48k Hz) or Ogg Opus, ≤8 MiB, ≤120 s, 21
languages auto-detected (no language param). Billed ¥0.39/hr (promo) — a
10 s command is ~¥0.001.

Rides ROCKYCODE_API_KEY (the home provider's key) — the same key the session
already talks to DeepSeek with, so voice is zero-setup wherever chat works.
The endpoint is NOT OpenAI-compatible, so it does not go through the
AsyncOpenAI client: one small httpx call (httpx is already a pinned dep).
Error codes map to one-line fixes, mirroring the localcheck preflight style.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx

ASR_URL = "https://api.deepseek.com/v1/tools/asr"

# (status → user-facing line). Not in _ERRORS (network, 5xx twice) → retryable
# generic message.
_ERRORS = {
    400: "the clip is not 16-bit PCM WAV or Ogg Opus (rocky always sends WAV — report this)",
    408: "the upload took too long (over 10 minutes) — retry on a steadier link",
    413: "the clip is over the 8 MiB limit — the recorder caps at 115 s, so this should not happen",
    429: "DeepSeek ASR is rate-limited (50 concurrent per account) — retry in a moment",
    503: "DeepSeek ASR is busy right now — retry in a moment",
    504: "DeepSeek ASR timed out recognizing the clip — a shorter clip may help",
}


class AsrError(Exception):
    """ASR failed. Message is user-facing (a fix, not a stack trace)."""


def home_key() -> tuple[Optional[str], str]:
    """(key, source name) of the home provider's official endpoint — the env
    var name goes into error messages, never the value (Endpoint.key_source)."""
    from rockycode.engine import providers as P
    prov = P.discover().get("deepseek")
    if prov is None or not prov.endpoints:
        return None, "ROCKYCODE_API_KEY"
    ep = prov.endpoints[0]
    key = ep.key()
    if key is None:
        return None, ep.key_source() or ep.key_env or "ROCKYCODE_API_KEY"
    return key, ep.key_source() or ep.key_env


async def transcribe(
    wav: bytes,
    *,
    key: str = "",
    filename: str = "voice.wav",
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 180.0,
) -> tuple[str, float]:
    """WAV bytes → (text, duration_ms). An injected `client` (mock transport
    in tests) is used as-is and not closed; otherwise a fresh client is made."""
    if not key:
        key, src = home_key()
        if not key:
            raise AsrError(f"no DeepSeek API key — set {src} (voice rides the home key)")
    own = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))
    try:
        resp = await client.post(
            ASR_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, wav, "audio/wav")},
        )
    except httpx.HTTPError as e:
        raise AsrError(f"could not reach DeepSeek ASR: {type(e).__name__} — check the network") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise AsrError(_ERRORS.get(resp.status_code,
                                   f"DeepSeek ASR failed ({resp.status_code}) — retry in a moment"))
    try:
        data = resp.json()
    except ValueError as e:
        raise AsrError("DeepSeek ASR sent an unreadable reply — retry in a moment") from e
    text = str(data.get("text", "")).strip()
    if not text:
        raise AsrError("the transcript came back empty — the clip was silent; speak a little louder")
    return text, float(data.get("duration_ms", 0))


def transcribe_path_sync(path: Path, *, key: str = "", timeout: float = 180.0) -> str:
    """Blocking variant for CLI flags (`rockycode chat --audio …`) — one sync
    call before the TUI starts (typer commands are sync; no event loop yet)."""
    if not key:
        key, src = home_key()
        if not key:
            raise AsrError(f"no DeepSeek API key — set {src} (voice rides the home key)")
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
            resp = client.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, path.read_bytes(), "audio/wav")},
            )
    except httpx.HTTPError as e:
        raise AsrError(f"could not reach DeepSeek ASR: {type(e).__name__} — check the network") from e
    if resp.status_code != 200:
        raise AsrError(_ERRORS.get(resp.status_code,
                                   f"DeepSeek ASR failed ({resp.status_code}) — retry in a moment"))
    try:
        text = str(resp.json().get("text", "")).strip()
    except ValueError as e:
        raise AsrError("DeepSeek ASR sent an unreadable reply — retry in a moment") from e
    if not text:
        raise AsrError("the transcript came back empty — the clip was silent; speak a little louder")
    return text
