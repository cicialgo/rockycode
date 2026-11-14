"""Voice pure core — EnergyVad endpointing, WAV wrapping, the TTS sentence
feeder, and the hotkey phase machine. No mic, no ffmpeg, no key, no network:
everything here is deterministic math over synthetic PCM.
"""
import os
import sys
import tempfile
import wave
from io import BytesIO
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.environ.pop("ROCKYCODE_API_KEY", None)  # hermetic: no ambient key

from rockycode.engine.voice import (  # noqa: E402
    MAX_RECORD_S,
    EnergyVad,
    SentenceFeeder,
    VoiceMachine,
    VoicePhase,
    pcm_to_wav,
    tone_samples,
)

def frames_of(ms: int) -> bytes:
    """*ms* milliseconds of digital silence (whole 10 ms frames)."""
    return b"\x00\x00" * (160 * ms // 10)


def test_vad_endpoints():
    vad = EnergyVad(threshold=700, attack_ms=60, release_ms=300, min_speech_ms=150)
    vad.feed(frames_of(100))                       # warm-up quiet
    assert vad.state == "silence" and not vad.ended
    vad.feed(tone_samples(220, 500, amp=8000))     # half a second of voice
    assert vad.state == "speech"
    vad.feed(frames_of(350))                       # trailing silence ≥ release
    assert vad.ended and vad.state == "silence"
    assert vad.ended_ms >= 500, vad.ended_ms       # the utterance's own duration
    print("  endpoint: speech → release-silence ends the utterance  ✓")


def test_vad_ignores_blips():
    vad = EnergyVad(threshold=700, attack_ms=60, release_ms=200, min_speech_ms=150)
    vad.feed(tone_samples(220, 100, amp=8000))     # shorter than min_speech_ms
    assert vad.state == "speech"                   # enters fast (barge-in needs that)
    vad.feed(frames_of(250))                       # …then released
    assert not vad.ended, "a blip must not end a recording"
    print("  blip: enters fast (barge-in), discards below min_speech_ms  ✓")


def test_vad_calibration():
    # Room noise below the configured threshold (RMS 283 < 700) lifts it to
    # 3× the noise floor (~850) — a voice at RMS 707 (amp 1000) would have
    # triggered the uncalibrated threshold and must NOT; RMS 990 (amp 1400)
    # beats the noise floor and must. Only frames BELOW the configured
    # threshold calibrate, so a user who starts talking immediately keeps the
    # configured threshold instead of calibrating to their own voice.
    vad = EnergyVad(threshold=700, attack_ms=60, release_ms=200, min_speech_ms=150)
    vad.feed(tone_samples(50, 300, amp=400))       # calibration window (quiet noise)
    assert vad.threshold > 800, vad.threshold
    vad.feed(tone_samples(220, 200, amp=1000))
    assert vad.state == "silence", f"voice below the noise floor: thr={vad.threshold}"
    vad.feed(frames_of(300))                       # clear the attack window
    vad.feed(tone_samples(220, 200, amp=1400))
    assert vad.state == "speech"
    print(f"  calibration: noise floor lifted the threshold to {vad.threshold}  ✓")


def test_pcm_to_wav():
    pcm = tone_samples(440, 1000, amp=8000)
    wav = pcm_to_wav(pcm)
    with wave.open(BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.readframes(w.getnframes()) == pcm
    assert len(wav) == len(pcm) + 44  # stdlib writes the canonical 44-byte header
    print("  wav: s16le PCM wrapped in a 44-byte WAV container  ✓")


def test_sentence_feeder():
    f = SentenceFeeder()
    assert f.feed("你好。世界！") == ["你好。", "世界！"]
    assert f.feed("还要") == []
    assert f.feed("继续。") == ["还要继续。"]          # deltas accumulate across feeds
    assert f.flush() == ""
    print("  feeder: CJK/ASCII enders accumulate across deltas  ✓")


def test_sentence_feeder_newline_and_cap():
    f = SentenceFeeder()
    assert f.feed("第一行\n第二行。") == ["第一行", "第二行。"]
    out = f.feed("x" * 300)                           # space-less run: hard cut at the cap
    assert out and max(len(s) for s in out) <= 240, [len(s) for s in out]
    assert f.flush()                                  # the remainder still flushes
    f2 = SentenceFeeder(max_chars=80)
    assert f2.feed("abc defgh " * 30)                 # spaced run: cut at a word boundary
    assert all(s and s[-1] != " " for s in f2.feed(""))
    print("  feeder: newline splits, cap cuts (word-boundary / hard)  ✓")


def test_sentence_feeder_drops_code():
    f = SentenceFeeder()
    assert f.feed("先看代码：\n```py\nprint('noisy')\n```\n说完。") == ["先看代码：", "说完。"]
    f2 = SentenceFeeder()
    f2.feed("```\n未闭合")                             # unclosed fence: dropped, flush says so
    assert f2.flush() == ""
    print("  feeder: fenced code blocks are not read aloud  ✓")


def test_machine():
    m = VoiceMachine(enabled=True)
    assert m.hotkey()[0] == "record"
    m.recording()
    assert m.phase == VoicePhase.RECORDING and m.hotkey()[0] == "stop"
    m.transcribing()
    assert m.hotkey()[0] == "busy"
    m.transcribed()
    assert m.phase == VoicePhase.IDLE
    m.speaking()
    assert m.phase == VoicePhase.SPEAKING and m.hotkey()[0] == "barge"
    m.cut()
    assert m.phase == VoicePhase.IDLE and not m.audible
    m.speaking()
    m.spoken()
    assert m.phase == VoicePhase.IDLE and not m.audible
    off = VoiceMachine(enabled=False)
    assert off.hotkey()[0] == "hint" and "voice_engine" in off.hotkey()[1]
    print("  machine: record/stop/busy/barge per phase; off → a hint  ✓")


def test_bounds():
    assert MAX_RECORD_S <= 120, "ASR takes ≤120 s per clip — the recorder must stop short"
    print("  bounds: recorder caps under the ASR 120 s limit  ✓")


test_vad_endpoints()
test_vad_ignores_blips()
test_vad_calibration()
test_pcm_to_wav()
test_sentence_feeder()
test_sentence_feeder_newline_and_cap()
test_sentence_feeder_drops_code()
test_machine()
test_bounds()
print("VOICE CORE SMOKE OK — VAD endpoint/blip/calibration · WAV · feeder · machine. amaze!")
