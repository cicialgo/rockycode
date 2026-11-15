"""DeepSeek TTS readback — text to speech on the home key, played locally.

POST https://api.deepseek.com/v1/tools/tts (JSON body, whole text): audio
streams back as it synthesizes, ≤10,000 billing chars per call (a Chinese
char counts 2), voices `stella` (女声, default) / `tide` (男声),
`filter_markdown` strips formatting server-side (default true). Billed
¥1.5 per 10k chars (promo) — a reply paragraph is well under ¥0.01. The
WebSocket streaming variant (audio while the model still generates) is NOT
used in phase 1: the SentenceFeeder already cuts the stream into sentences,
so one POST per sentence reads naturally and keeps interruption simple.

Playback is one subprocess per sentence (afplay on macOS, ffplay / sox
elsewhere) — `Player.stop()` kills it, which is the whole barge-in
mechanism: cut the audio the instant the user starts talking. Subprocess
playback also means no audio-library dependency in the install.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx

TTS_URL = "https://api.deepseek.com/v1/tools/tts"
VOICES = ("stella", "tide")

_ERRORS = {
    400: "the text was empty or refused by the content filter — that sentence is skipped",
    429: "DeepSeek TTS is rate-limited (50 concurrent per account) — skipping readback this turn",
    503: "DeepSeek TTS is busy — readback paused; the transcript keeps the text",
    504: "DeepSeek TTS timed out — readback paused; the transcript keeps the text",
}

# One candidate player per platform (argv prefix, resolved with which()).
# afplay ships with macOS; ffplay ships with ffmpeg (already rocky's image
# scaler); `play` is sox. Any of them plays a .wav file; the last two need
# their silent flags so a readback never fills a log.
_PLAYERS = (
    ("afplay",),
    ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"),
    ("play", "-q"),
)


class TtsError(Exception):
    """TTS failed. Message is user-facing. `permanent` = retrying won't help
    (no key, no player binary) — the app turns readback off for the session;
    transient errors (429/503/504) only pause the current turn."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def pick_player() -> Optional[tuple[str, ...]]:
    """The first installed player argv, or None (readback cannot sound)."""
    for argv in _PLAYERS:
        if shutil.which(argv[0]):
            return argv
    return None


async def synthesize(
    text: str,
    *,
    key: str = "",
    voice_id: str = "stella",
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 60.0,
) -> bytes:
    """text → wav bytes. An injected `client` (mock transport in tests) is
    used as-is and not closed."""
    if voice_id not in VOICES:
        voice_id = "stella"
    if not key:
        from rockycode.engine import asr as asr_mod  # home key lives there
        key, src = asr_mod.home_key()
        if not key:
            raise TtsError(f"no DeepSeek API key — set {src} (readback rides the home key)",
                           permanent=True)
    own = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))
    try:
        resp = await client.post(
            TTS_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"text": text, "voice_id": voice_id},
        )
    except httpx.HTTPError as e:
        raise TtsError(f"could not reach DeepSeek TTS: {type(e).__name__} — check the network") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise TtsError(_ERRORS.get(resp.status_code,
                                   f"DeepSeek TTS failed ({resp.status_code}) — readback paused this turn"))
    if not resp.content or len(resp.content) < 44:
        raise TtsError("DeepSeek TTS returned an empty clip — readback paused this turn")
    return resp.content


class Player:
    """Plays one .wav at a time; stop() kills it instantly (barge-in).

    A temp file per sentence (afplay needs a path; ffplay could stream but
    one mechanism for all three players keeps it simple). stop() is
    idempotent and safe to call from any phase — including with nothing
    playing.
    """

    def __init__(self, argv: Optional[tuple[str, ...]] = None) -> None:
        self._argv = argv if argv is not None else pick_player()
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None

    def available(self) -> bool:
        return self._argv is not None

    def missing_hint(self) -> str:
        return ("no audio player installed — afplay (macOS), ffplay (ffmpeg) or sox "
                "is needed for voice readback")

    def speak(self, wav: bytes) -> bool:
        """Start playing. False = could not start (nothing playing)."""
        if self._argv is None:
            return False
        self.stop()
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="rockyvoice-")
        path = Path(name)
        try:
            with open(fd, "wb") as f:
                f.write(wav)
            self._proc = subprocess.Popen(
                [*self._argv, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            path.unlink(missing_ok=True)
            return False
        self._path = path
        return True

    def wait(self) -> None:
        """Block until the current clip finishes (call via to_thread)."""
        if self._proc is not None:
            self._proc.wait()
            self._cleanup()

    def stop(self) -> None:
        """Kill the current clip now; idempotent."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        self._cleanup()

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _cleanup(self) -> None:
        if self._proc is not None:
            self._proc = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None
