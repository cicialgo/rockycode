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

import io
import os
import shutil
import subprocess
import tempfile
import time
import wave
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
# A 400 is ONE sentence refused, not a turn-level failure — the flag lets
# the speak loop skip it and keep reading the rest.
_SKIPPABLE = {400}

def _ffplay_candidates() -> list[str]:
    """ffplay lives BESIDE ffmpeg — a pinned ROCKYCODE_FFMPEG implies its
    sibling first (a machine can carry a broken ffmpeg early on PATH, and
    the ffplay next to it is broken too; the probe in pick_player skips
    both, and afplay takes over)."""
    out = ["ffplay"]
    pinned = (os.getenv("ROCKYCODE_FFMPEG") or "").strip()
    if pinned and Path(pinned).name == "ffmpeg":
        sibling = str(Path(pinned).with_name("ffplay"))
        if sibling not in out:
            out.insert(0, sibling)
    return out


def _runs(argv: list[str]) -> bool:
    """Does this candidate actually execute? A broken binary (dyld errors)
    must not win and then fail silently mid-sentence."""
    try:
        return subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3.0,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_player() -> Optional[tuple[str, ...]]:
    """The first player that ACTUALLY RUNS. ffplay first (the only one
    that can stream raw PCM over a pipe — gapless multi-sentence
    playback); afplay (macOS) and sox's `play` fall back to
    one-process-per-clip file mode."""
    for cand in _ffplay_candidates():
        path = shutil.which(cand)
        if path and _runs([path, "-version"]):
            return (path, "-nodisp", "-autoexit", "-loglevel", "quiet")
    for cand in ("afplay", "play"):
        if shutil.which(cand):
            return (cand, "-q") if cand == "play" else (cand,)
    return None


def wav_parts(wav: bytes) -> tuple[int, bytes]:
    """(sample rate, raw PCM) from a WAV clip — the pipe-streaming player
    feeds ffplay raw PCM (concatenated WAV files are NOT a valid stream:
    the second RIFF header would be played as audio). A clip that isn't a
    WAV at all is defensive noise — nothing to feed."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            return w.getframerate() or 24_000, w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return 24_000, b""


class TtsError(Exception):
    """TTS failed. Message is user-facing. `permanent` = retrying won't help
    (no key, no player binary) — the app turns readback off for the session;
    transient errors (429/503/504) only pause the current turn; `skippable`
    = the content filter refused THIS ONE sentence — skip it and keep
    reading (never a reason to drop the tail)."""

    def __init__(self, message: str, *, permanent: bool = False,
                 skippable: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.skippable = skippable


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
                                   f"DeepSeek TTS failed ({resp.status_code}) — readback paused this turn"),
                       skippable=resp.status_code in _SKIPPABLE)
    if not resp.content or len(resp.content) < 44:
        raise TtsError("DeepSeek TTS returned an empty clip — readback paused this turn")
    return resp.content


class Player:
    """Plays clips; stop() kills it instantly (barge-in).

    Two modes:
    - STREAM mode (ffplay): one persistent process reads raw PCM from a
      pipe — consecutive clips play back CONTIGUOUSLY with no per-sentence
      process spawns or gaps (a bulleted reply is a dozen tiny sentences;
      a spawn each would stutter). feed() opens the stream lazily on the
      first clip (the sample rate is only known then), finish_stream()
      closes stdin so ffplay drains the buffer and exits (-autoexit).
    - FILE mode (afplay / sox): one temp .wav per clip, one process per
      clip — the fallback when ffplay is missing.

    stop() is idempotent and safe to call from any phase — including with
    nothing playing."""

    def __init__(self, argv: Optional[tuple[str, ...]] = None) -> None:
        self._argv = argv if argv is not None else pick_player()
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._streaming = False
        self._fed_s = 0.0       # stream mode: total audio seconds fed
        self._started_at = 0.0  # stream mode: when the pipe process spawned

    def available(self) -> bool:
        return self._argv is not None

    def streamable(self) -> bool:
        """ffplay (and only ffplay) can take a raw-PCM pipe. The argv head
        may be a full path — compare the basename."""
        return self._argv is not None and os.path.basename(self._argv[0]) == "ffplay"

    def missing_hint(self) -> str:
        return ("no audio player installed — afplay (macOS), ffplay (ffmpeg) or sox "
                "is needed for voice readback")

    def speak(self, wav: bytes) -> bool:
        """FILE mode: play one clip. False = could not start."""
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

    def feed(self, pcm: bytes, rate: int) -> None:
        """STREAM mode: push one clip's PCM into the pipe (call via
        to_thread — the write blocks when ffplay's buffer is full, which
        is exactly the backpressure that keeps playback gapless). The
        stream opens lazily on the first clip, at that clip's rate."""
        if not self.streamable():
            return
        if self._proc is None or self._proc.poll() is not None:
            # NOTE ffplay (unlike ffmpeg) rejects `-ac` — "Option not found"
            # kills it instantly; -ch_layout is the ffplay spelling.
            self._proc = subprocess.Popen(
                [*self._argv, "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono", "-i", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._streaming = True
            self._fed_s = 0.0
            self._started_at = time.monotonic()
        if self._proc.stdin is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.write(pcm)
                self._proc.stdin.flush()
                self._fed_s += len(pcm) / rate / 2
            except (BrokenPipeError, OSError):
                pass  # the process died — the next feed or drain notices

    def finish_stream(self) -> None:
        """STREAM mode: the last clip was fed — close stdin. NOTE ffplay
        exits at pipe EOF WITHOUT draining its queue (raw PCM has no
        duration), so finishing is the caller's pacing job — see drain()."""
        if self._streaming and self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._streaming = False

    def remaining_s(self) -> float:
        """STREAM mode: how much of the fed stream has yet to play (the
        paced estimate — ffplay's queue holds everything the pipe accepted
        ahead of playback). The caller paces this out with a CANCELLABLE
        async sleep: a thread-sleep here would join-block asyncio.run()
        shutdown for the whole tail (the live ctrl+q hang)."""
        if self._fed_s <= 0:
            return 0.0
        return max(0.0, self._fed_s - (time.monotonic() - self._started_at))

    def wait(self) -> None:
        """Block until playback finishes (call via to_thread)."""
        if self._proc is not None:
            self._proc.wait()
            self._cleanup()

    def stop(self) -> None:
        """Kill playback now (barge-in); idempotent."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        self._cleanup()

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _cleanup(self) -> None:
        if self._proc is not None:
            self._proc = None
        self._streaming = False
        self._fed_s = 0.0
        self._started_at = 0.0
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None
