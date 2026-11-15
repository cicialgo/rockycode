"""Talk mode in the TUI (headless Textual pilot, scripted mic + fake ASR,
no mic, no ffmpeg, no key, no network).

Paths driven through the real RockyCodeApp:
  A) /voice talk on → chip lights; a spoken utterance (scripted PCM) ends →
     transcribes → AUTO-SUBMITS (no Enter) → the fake engine answers →
     the conversation auto-listens again.
  B) speech during the reply → BARGE: the audio path cuts, the turn is
     cancelled, listening starts; the machine counts the barge.
  C) the hotkey pauses a running talk; /voice talk on resumes it.
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
os.chdir(tempfile.mkdtemp(prefix="rockytuitalk-"))

from textual.widgets import Static  # noqa: E402

import rockycode.engine.asr as asr_mod  # noqa: E402
import rockycode.engine.recorder as rec_mod  # noqa: E402
import rockycode.engine.voice as voice_mod  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.tui.app import ChatInput, RockyCodeApp  # noqa: E402

WD = Path.cwd()
TONE = voice_mod.tone_samples(220, 500, amp=8000)   # 0.5 s of "voice"
SIL_300 = b"\x00\x00" * (160 * 30)                  # 0.3 s of silence
SIL_900 = b"\x00\x00" * (160 * 90)                  # 0.9 s — ends an utterance


def chunk(content=None, usage=None):
    d = types.SimpleNamespace(reasoning_content=None, content=content, tool_calls=None)
    return types.SimpleNamespace(usage=usage, choices=[types.SimpleNamespace(delta=d)])


class U:
    def model_dump(self):
        return {"prompt_tokens": 30, "completion_tokens": 6}


async def stream(chunks):
    for c in chunks:
        yield c


class Script:
    """One scripted model response per API call; stall(ev) holds the reply
    open until the test releases the event."""
    def __init__(self):
        self.q = deque()

    def text(self, s):
        self.q.append([chunk(content=s), chunk(usage=U())])
        return self

    def stall(self, event):
        self.q.append(("stall", event))
        return self

    async def create(self, **kwargs):
        if not self.q:
            return stream([chunk(content="ok."), chunk(usage=U())])
        item = self.q.popleft()
        if isinstance(item, tuple):
            await item[1].wait()
            return stream([chunk(content="ok."), chunk(usage=U())])
        return stream(item)


class TalkRec:
    """The scripted warm mic: read_new drains whatever the test pushed (the
    bytes flow through mark()/finish_segment() like the real buffer);
    `hot` holds the device alive, like a live ffmpeg process."""

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


def build(script=None):
    script = script or Script()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    engine = Engine(model="fake", client=client, workdir=WD, registry={})
    app = RockyCodeApp(engine)
    return app, engine, script


def txt(app):
    return " ".join(str(w.render()) for w in app.query(Static))


async def wait_until(pilot, cond, timeout=8.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def _fake_transcribe(wav, **_kw):
    return "把测试跑一遍", 1500.0


async def test_talk_conversation():
    asr_mod.transcribe = _fake_transcribe
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0  # the scripted mic idles — no stall interference
    script = Script().text("收到，继续。")
    app, _, _ = build(script=script)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "ctrl+g" in txt(app))
        await pilot.press(*"/voice talk on")
        await pilot.press("enter")
        ok = await wait_until(pilot, lambda: "talk · listening" in txt(app))
        assert ok, txt(app)
        assert app._talk.phase == "listening"
        # speak: utterance → end-silence → ASR → AUTO-SUBMIT (no Enter)
        app._recorder.q.extend([TONE, SIL_300, SIL_900])
        ok = await wait_until(pilot, lambda: "把测试跑一遍" in txt(app))
        assert ok, txt(app)                       # the user turn auto-added
        ok = await wait_until(pilot, lambda: "收到，继续。" in txt(app))
        assert ok, txt(app)                       # the engine answered
        ok = await wait_until(pilot, lambda: app._talk.phase == "listening")
        assert ok, app._talk.phase                # and listened again
    voice_mod.STALL_S = 3.0
    print("  talk: utterance auto-sends, reply lands, the mic listens again  ✓")


async def test_talk_barge_in():
    asr_mod.transcribe = _fake_transcribe
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0
    gate = asyncio.Event()
    script = Script().stall(gate)  # the reply hangs open — rocky is "thinking"
    app, _, _ = build(script=script)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "ctrl+g" in txt(app))
        await pilot.press(*"/voice talk on")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app._talk.phase == "listening")
        app._recorder.q.extend([TONE, SIL_300, SIL_900])
        ok = await wait_until(pilot, lambda: app._talk.phase == "thinking")
        assert ok, app._talk.phase                # the turn is running…
        app._recorder.q.append(TONE)              # …and the user speaks over it
        ok = await wait_until(pilot, lambda: "interrupted" in txt(app))
        assert ok, txt(app)                       # BARGE
        assert app._talk.phase == "listening", app._talk.phase
        assert app._talk.barges == 1
        gate.set()                                # release the reply — no hang
        ok = await wait_until(pilot, lambda: "turn cancelled" in txt(app))
        assert ok, txt(app)
    voice_mod.STALL_S = 3.0
    print("  barge: speech during the turn cuts it, cancels, and listens  ✓")


async def test_talk_hotkey_pauses():
    asr_mod.transcribe = _fake_transcribe
    rec_mod.Recorder = TalkRec
    voice_mod.STALL_S = 300.0
    app, _, _ = build()
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "ctrl+g" in txt(app))
        await pilot.press(*"/voice talk on")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app._talk.phase == "listening")
        await pilot.press("ctrl+g")               # the hotkey pauses talk
        ok = await wait_until(pilot, lambda: "talk paused" in txt(app))
        assert ok, txt(app)
        assert app._talk.phase == "paused"
        await pilot.press(*"/voice talk on")      # resume by command
        await pilot.press("enter")
        ok = await wait_until(pilot, lambda: app._talk.phase == "listening")
        assert ok, app._talk.phase
    voice_mod.STALL_S = 3.0
    print("  pause: the hotkey pauses talk; /voice talk on resumes  ✓")


def run():
    asyncio.run(test_talk_conversation())
    asyncio.run(test_talk_barge_in())
    asyncio.run(test_talk_hotkey_pauses())


if __name__ == "__main__":
    run()
    print("TUI TALK SMOKE OK — auto-turn · barge · pause/resume. amaze!")
