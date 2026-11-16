"""ctrl+q teardown — the process must EXIT (found live: quitting while
readback played respawned a listen worker mid-shutdown, whose transcript
mount crashed and held the process).

Two hang shapes, driven through the real RockyCodeApp with fake
recorder/ASR/TTS:
  A) speaking: the TTS synth is in flight (speak task alive) at ctrl+q —
     its finally must not hand the conversation back.
  B) thinking: the reply stream is mid-flight at ctrl+q.
"""
import asyncio
import os
import sys
import tempfile
import types
from collections import deque
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.environ.pop("ROCKYCODE_API_KEY", None)  # hermetic: no ambient key
os.chdir(tempfile.mkdtemp(prefix="rockytuiexit-"))

import rockycode.engine.asr as asr_mod  # noqa: E402
import rockycode.engine.recorder as rec_mod  # noqa: E402
import rockycode.engine.tts as tts_mod  # noqa: E402
import rockycode.engine.voice as voice_mod  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402

REAL_HOME_KEY = asr_mod.home_key
REAL_SYNTH = tts_mod.synthesize
REAL_STALL = voice_mod.STALL_S

WD = Path.cwd()
TONE = voice_mod.tone_samples(220, 500, amp=8000)
SIL_300 = b"\x00\x00" * (160 * 30)
SIL_900 = b"\x00\x00" * (160 * 90)


class TalkRec:
    """The scripted warm mic (same shape as smoke_tui_talk's)."""
    stop_requested = False
    device = "fake"
    hot = True

    def __init__(self):
        self.q = deque()
        self._buf = bytearray()
        self._mark = 0
        self._vad_cur = 0

    @property
    def running(self):
        return self.hot

    async def start(self):
        return None

    def mark(self):
        self._mark = len(self._buf)
        self._vad_cur = max(self._vad_cur, self._mark)

    def read_new(self):
        while self.q:
            self._buf.extend(self.q.popleft())
        if self._vad_cur >= len(self._buf):
            return b""
        data = bytes(self._buf[self._vad_cur:])
        self._vad_cur = len(self._buf)
        return data

    def finish_segment(self):
        seg = bytes(self._buf[self._mark:])
        self._mark = len(self._buf)
        return seg

    async def stop(self):
        self.q.clear()

    def stderr_tail(self):
        return ""


def chunk(content=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=None)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


async def stream(chunks):
    for c in chunks:
        yield c


class Script:
    def __init__(self, thinking: bool):
        self.thinking = thinking

    async def create(self, **kwargs):
        if self.thinking:
            async def gen():  # one chunk, then the stream hangs mid-reply
                yield chunk(content="收到。")
                await asyncio.sleep(3600)
            return gen()
        return stream([chunk(content="收到。")])


async def fake_transcribe(wav, **_kw):
    return "把测试跑一遍", 1500.0


async def slow_synth(text, **kw):
    await asyncio.sleep(3600)  # the speak task is alive at exit
    return b"RIFF" + b"\x00" * 60


async def instant_synth(text, **kw):
    return b"RIFF" + b"\x00" * 60


class FakePlayer:
    """Stands in for the ffplay stream player with a MINUTES-long tail —
    the paced-out sleep must be cancellable, or ctrl+q join-blocks in the
    executor thread (the second live hang)."""

    def __init__(self):
        self.stopped = 0

    def available(self):
        return True

    def missing_hint(self):
        return "fake"

    def streamable(self):
        return True

    def speak(self, wav):
        return True

    def feed(self, pcm, rate):
        pass

    def finish_stream(self):
        pass

    def remaining_s(self):
        return 3600.0  # a minutes-long spoken tail

    def wait(self):
        pass

    def stop(self):
        self.stopped += 1

    @property
    def playing(self):
        return False


async def test_exit_while_speaking():
    asr_mod.transcribe = fake_transcribe
    asr_mod.home_key = lambda: ("sk-test", "T")
    tts_mod.synthesize = slow_synth
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Script(False)))
    app = RockyCodeApp(Engine(model="fake", client=client, workdir=WD, registry={}),
                       voice_readback="on")
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)
            await pilot.press(*"/voice talk on")
            await pilot.press("enter")
            await pilot.pause(0.5)
            app._recorder.q.extend([TONE, SIL_300, SIL_900])
            for _ in range(200):
                await pilot.pause(0.05)
                if app._talk.phase == "speaking":
                    break
            assert app._talk.phase == "speaking", app._talk.phase
            await pilot.press("ctrl+q")
    finally:
        asr_mod.home_key = REAL_HOME_KEY
        tts_mod.synthesize = REAL_SYNTH
        voice_mod.STALL_S = REAL_STALL
    print("  exit while speaking: ctrl+q returns cleanly  ✓")


async def test_exit_while_draining():
    # THE second live hang: exit while the tail's drain sleep paces out the
    # buffered audio. A thread-sleep there joins asyncio.run() shutdown for
    # the whole remaining tail; the async sleep is cancelled instantly and
    # the player is stopped.
    asr_mod.transcribe = fake_transcribe
    asr_mod.home_key = lambda: ("sk-test", "T")
    tts_mod.synthesize = instant_synth
    real_player, tts_mod.Player = tts_mod.Player, FakePlayer
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Script(False)))
    app = RockyCodeApp(Engine(model="fake", client=client, workdir=WD, registry={}),
                       voice_readback="on")
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)
            await pilot.press(*"/voice talk on")
            await pilot.press("enter")
            await pilot.pause(0.5)
            app._recorder.q.extend([TONE, SIL_300, SIL_900])
            for _ in range(200):
                await pilot.pause(0.05)
                if app._talk.phase == "speaking" and app._voice_player is not None:
                    break
            assert app._voice_player is not None, "the stream player must be live"
            await pilot.press("ctrl+q")  # must return instantly, not in ~1 h
        assert app._voice_player.stopped >= 1, "the tail must be cut at exit"
    finally:
        asr_mod.home_key = REAL_HOME_KEY
        tts_mod.synthesize = REAL_SYNTH
        tts_mod.Player = real_player
        voice_mod.STALL_S = REAL_STALL
    print("  exit while draining: the tail cut is cancellable — instant quit  ✓")


async def test_exit_while_thinking():
    asr_mod.transcribe = fake_transcribe
    asr_mod.home_key = lambda: ("sk-test", "T")
    tts_mod.synthesize = slow_synth
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Script(True)))
    app = RockyCodeApp(Engine(model="fake", client=client, workdir=WD, registry={}),
                       voice_readback="on")
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)
            await pilot.press(*"/voice talk on")
            await pilot.press("enter")
            await pilot.pause(0.5)
            app._recorder.q.extend([TONE, SIL_300, SIL_900])
            for _ in range(200):
                await pilot.pause(0.05)
                if app._talk.phase == "thinking":
                    break
            assert app._talk.phase == "thinking", app._talk.phase
            await pilot.press("ctrl+q")
    finally:
        asr_mod.home_key = REAL_HOME_KEY
        tts_mod.synthesize = REAL_SYNTH
        voice_mod.STALL_S = REAL_STALL
    print("  exit while thinking: ctrl+q returns cleanly  ✓")


def run():
    asyncio.run(test_exit_while_speaking())
    asyncio.run(test_exit_while_thinking())
    asyncio.run(test_exit_while_draining())


if __name__ == "__main__":
    run()
    print("TUI EXIT SMOKE OK — ctrl+q during speaking/thinking/draining tears down cleanly. amaze!")
