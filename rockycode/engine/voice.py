"""Voice input & readback — the PURE core. No asyncio, no Textual, no I/O.

Phase 1: a hotkey records the mic (engine/recorder.py, ffmpeg), an energy
VAD here does the endpointing (when did the user stop talking?), the clip
goes to DeepSeek ASR (engine/asr.py) and the transcript lands in the input
box; optional readback speaks rocky's replies aloud (engine/tts.py) and a
hotkey press during playback barges in — cut the audio, start listening,
exactly like talking over a person.

The I/O layers drive this module with events; everything here is pure and
deterministic so the rules are smoke-testable without a mic, ffmpeg, a key,
or a network. Same split as cron.py (bookkeeping here, driving there).

Bounds (asr docs, 2026-11): DeepSeek ASR takes ≤120 s per clip, so the
recorder auto-stops at MAX_RECORD_S. No streaming — the clip is one request;
the VAD's job is to make that clip one utterance.
"""
from __future__ import annotations

import io
import math
import re
import wave
from typing import Callable, Optional

# DeepSeek ASR caps: ≤120 s per clip, ≤8 MiB. Stop short of the cap so a slow
# stop never pushes the clip over.
MAX_RECORD_S = 115.0
# A live mic delivers bytes even in silence (digital zeros ARE bytes) — so
# zero bytes for this long means the device stalled (Bluetooth switch, USB
# unplug, a dead reader task), not quiet. The capture restarts the mic once.
STALL_S = 3.0
SAMPLE_RATE = 16_000        # the recorder's raw PCM rate (ASR accepts 8k–48k)
FRAME_S = 0.010             # VAD frame length — 10 ms (160 samples @16k)

# A sentence to read aloud is cut at MAX_SENT_CHARS if no ender arrives —
# bounds the audio-latency of one TTS call and one playback buffer.
MAX_SENT_CHARS = 240

# Sentence enders for the TTS feeder. CJK + ASCII enders plus the ellipsis;
# closing quotes/brackets after an ender are consumed with it.
_SENT_ENDERS = ("。", "！", "？", "!", "?", "…", "；", ";")
_SENT_CLOSERS = set('"\'”’」』）)】》]`')


class VoicePhase:
    """One voice phase. The chip in the status bar renders this."""

    OFF = "off"                    # feature disabled (/config voice_engine off)
    IDLE = "idle"
    RECORDING = "recording"        # mic hot, VAD looking for the end
    TRANSCRIBING = "transcribing"  # clip uploaded to ASR
    SPEAKING = "speaking"          # readback audible

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return (cls.OFF, cls.IDLE, cls.RECORDING, cls.TRANSCRIBING, cls.SPEAKING)


# Hotkey semantics — what a press MEANS in each phase. The machine decides,
# the app executes (record worker / player cut / hint line).
HINT, RECORD, STOP, BUSY, BARGE = "hint", "record", "stop", "busy", "barge"


class VoiceMachine:
    """Phase keeper. One rule lives here: what the hotkey does now.

    - OFF          → a hint line (never a recording: the feature is off)
    - IDLE         → start recording
    - RECORDING    → stop the recorder (manual endpoint)
    - TRANSCRIBING → busy — one clip at a time; the press is ignored
    - SPEAKING     → BARGE: the app cuts the audio and starts recording
                     (talk over rocky — like a person)

    The turn is NOT cancelled on a barge: the user may be dictating the next
    message while the reply keeps streaming silently. Esc still cancels.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.phase = VoicePhase.IDLE if enabled else VoicePhase.OFF
        self.audible = False  # readback currently sounding (any phase)

    def hotkey(self) -> tuple[str, str]:
        """(action, message) for a hotkey press in the current phase."""
        if self.phase == VoicePhase.OFF:
            return HINT, "voice is off — /config voice_engine deepseek (then the hotkey records)"
        if self.phase == VoicePhase.RECORDING:
            return STOP, ""
        if self.phase == VoicePhase.TRANSCRIBING:
            return BUSY, "still transcribing — one moment"
        if self.phase == VoicePhase.SPEAKING:
            return BARGE, ""
        return RECORD, ""

    def recording(self) -> None:
        self.phase = VoicePhase.RECORDING

    def transcribing(self) -> None:
        self.phase = VoicePhase.TRANSCRIBING

    def transcribed(self) -> None:
        """A clip resolved (text or error) — back to idle."""
        self.phase = VoicePhase.IDLE

    def speaking(self) -> None:
        self.phase = VoicePhase.SPEAKING
        self.audible = True

    def spoken(self) -> None:
        self.audible = False
        if self.phase == VoicePhase.SPEAKING:
            self.phase = VoicePhase.IDLE

    def cut(self) -> None:
        """Readback killed (barge-in / cancel / new turn). Recording state is
        untouched — a barge falls through into recording."""
        self.audible = False
        if self.phase == VoicePhase.SPEAKING:
            self.phase = VoicePhase.IDLE


class EnergyVad:
    """Energy-RMS voice activity detector on 16-bit mono PCM.

    Feed frames of raw little-endian int16 via feed(); the detector reports
    state ("silence" | "speech") and latches .ended when one utterance
    completes — the recorder stops there. Rules:

    - a 10 ms frame is "loud" when its RMS beats the threshold
    - speech needs loud frames spanning attack_ms (a key clack is ~10 ms and
      is ignored; 60 ms of voice is not)
    - the utterance ends after release_ms of trailing quiet frames
    - an utterance shorter than min_speech_ms is discarded as a blip
    - the first CALIBRATE_FRAMES quiet frames calibrate the threshold to
      3× the measured noise floor (never below the configured value), so a
      hot mic and a quiet mic both endpoint — only frames BELOW the initial
      threshold calibrate, so a user who starts talking immediately keeps the
      configured threshold instead of calibrating to their own voice.
    """

    CALIBRATE_FRAMES = 30  # 300 ms

    def __init__(
        self,
        *,
        threshold: int = 700,       # RMS×32768; 700 ≈ -33 dBFS
        attack_ms: int = 60,
        release_ms: int = 700,      # endpointing: "stopped talking"
        min_speech_ms: int = 200,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.threshold = threshold
        self._attack_ms = max(attack_ms, 10)
        self._release_ms = max(release_ms, 10)
        self._min_speech_ms = max(min_speech_ms, 10)
        self._on_state = on_state
        self._buf = b""
        self.state = "silence"
        self.speech_ms = 0          # in-flight utterance span (since speech began)
        self.loud_ms = 0            # the LOUD part of it — min_speech_ms checks this
        self.silence_ms = 0         # trailing quiet since the last loud frame
        self.ended = False          # latch: one complete utterance
        self.ended_ms = 0           # that utterance's speech duration ("too short")
        self._frames = 0
        self._loud_ms = 0
        self._cal_noise = []        # RMS of quiet calibration frames
        self._calibrated = False
        self._state_emitted = "silence"

    def _framesize(self) -> int:
        return int(SAMPLE_RATE * FRAME_S) * 2  # 320 bytes = 160 samples

    def feed(self, data: bytes) -> None:
        self._buf += data
        fs = self._framesize()
        while len(self._buf) >= fs:
            frame, self._buf = self._buf[:fs], self._buf[fs:]
            self._frame(frame)

    def _frame(self, frame: bytes) -> None:
        # RMS of int16 samples without decoding the whole frame (le array).
        n = len(frame) // 2
        acc = 0
        for i in range(0, n * 2, 2):
            lo, hi = frame[i], frame[i + 1]
            v = lo | (hi << 8)
            if v >= 0x8000:
                v -= 0x10000
            acc += v * v
        rms = math.sqrt(acc / n) if n else 0.0
        self._frames += 1
        t = FRAME_S * 1000

        if not self._calibrated:
            if rms <= self.threshold and len(self._cal_noise) < self.CALIBRATE_FRAMES:
                self._cal_noise.append(rms)
            if self._frames >= self.CALIBRATE_FRAMES:
                if self._cal_noise:
                    floor = sum(self._cal_noise) / len(self._cal_noise)
                    self.threshold = int(max(self.threshold, min(30000, 3 * floor + 1)))
                self._calibrated = True

        loud = rms > self.threshold
        if self.state == "silence":
            if loud:
                self._loud_ms += t
                if self._loud_ms >= self._attack_ms:
                    self._enter_speech()
            else:
                self._loud_ms = 0
        else:  # speech
            self.speech_ms += t
            if loud:
                self.loud_ms += t
                self.silence_ms = 0
            else:
                self.silence_ms += t
                if self.silence_ms >= self._release_ms:
                    if self.loud_ms >= self._min_speech_ms:
                        self._end_speech()
                    else:
                        self._enter_silence()  # a blip — discarded

    def _enter_speech(self) -> None:
        self.state = "speech"
        self.speech_ms = self._loud_ms
        self.loud_ms = self._loud_ms
        self.silence_ms = 0
        self.ended = False
        self._emit()

    def _end_speech(self) -> None:
        self.ended_ms = self.loud_ms
        self._enter_silence()
        self.ended = True

    def _enter_silence(self) -> None:
        self.state = "silence"
        self.speech_ms = 0
        self.loud_ms = 0
        self.silence_ms = 0
        self._loud_ms = 0
        self._emit()

    def _emit(self) -> None:
        if self.state != self._state_emitted and self._on_state is not None:
            self._state_emitted = self.state
            self._on_state(self.state)


def tone_samples(freq_hz: float, ms: int, *, amp: int = 8000) -> bytes:
    """A sine wave of *freq_hz* for *ms* — test PCM for the VAD, and a handy
    calibrator when a user wants to check a mic's level."""
    import array
    n = int(SAMPLE_RATE * ms / 1000)
    out = array.array("h")
    for i in range(n):
        out.append(int(amp * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE)))
    return out.tobytes()


def pcm_to_wav(pcm: bytes, *, rate: int = SAMPLE_RATE, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw s16le PCM in a WAV container (stdlib wave) — the ASR upload."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


_FENCE_RE = re.compile(r"^\s*```")


class SentenceFeeder:
    """Reply deltas → complete sentences to read aloud (pure).

    Splits on sentence enders (。！？!?…；;) and on newlines (a markdown
    list/paragraph boundary reads naturally as a pause); a sentence with no
    ender is cut at MAX_SENT_CHARS so one TTS call never lags too far behind
    the stream. Fenced code blocks are dropped — reading code aloud is noise;
    `filter_markdown` on the TTS endpoint strips the rest.

    Roughness is fine by design: TTS is a convenience voice, the transcript
    keeps the exact text.
    """

    def __init__(self, max_chars: int = MAX_SENT_CHARS) -> None:
        self._max_chars = max(40, max_chars)
        self._buf = ""
        self._fence = False
        self._pending: list[str] = []

    def feed(self, delta: str) -> list[str]:
        self._pending.clear()
        if not delta:
            return []
        i = 0
        lines = delta.split("\n")
        for j, line in enumerate(lines):
            if j:
                self._line_end()  # every \n is a sentence boundary
            if _FENCE_RE.match(line):
                self._fence = not self._fence
                continue
            if self._fence:
                continue
            i = 0
            while i < len(line):
                ch = line[i]
                if ch in _SENT_ENDERS:
                    # consume trailing closers with the sentence
                    k = i + 1
                    while k < len(line) and line[k] in _SENT_CLOSERS:
                        k += 1
                    self._buf += line[i:k]
                    self._emit()
                    i = k
                else:
                    self._buf += ch
                    i += 1
                    if len(self._buf) >= self._max_chars:
                        self._cut()
        return list(self._pending)

    def flush(self) -> str:
        """The remainder at turn end — read the tail too."""
        if self._fence:
            self._fence = False  # an unclosed fence ends the drop
        rest = self._buf.strip()
        self._buf = ""
        return rest

    def _line_end(self) -> None:
        if not self._fence:
            self._emit()

    def _cut(self) -> None:
        """No ender within the cap: cut at the last space inside the cap (or
        hard-cut a space-less run like a base64 blob) and keep the remainder
        accumulating for the next sentence."""
        head = self._buf[: self._max_chars]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        tail = self._buf[len(head):].lstrip()
        self._buf = head
        self._emit()
        self._buf = tail

    def _emit(self) -> None:
        text = self._buf.strip()
        self._buf = ""
        if text:
            self._pending.append(text)
