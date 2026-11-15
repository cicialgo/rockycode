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


if __name__ == "__main__":
    run()
    print("TUI EXIT SMOKE OK — ctrl+q during speaking/thinking tears down cleanly. amaze!")
