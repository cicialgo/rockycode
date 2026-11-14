"""Voice in the TUI (headless Textual pilot, fake recorder + fake ASR, no
mic, no ffmpeg, no key, no network).

Paths driven through the real RockyCodeApp:
  A) voice_engine off → ctrl+g prints the hint, never records; chip says off.
  B) on → ctrl+g records (fake recorder) → transcript lands in the input box
     (Enter still sends); the chip walks listening → … → idle.
  C) voice_autosend on → the transcript is submitted as a turn; the fake
     engine answers.
  D) barge-in: readback speaking → ctrl+g cuts it (audible False) and starts
     recording; a second press stops it; the transcript lands.
  E) busy: a second press while transcribing says "still transcribing".
  F) readback with no key → one permanent line, readback off for the session
     (the transcript keeps the text).
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
os.chdir(tempfile.mkdtemp(prefix="rockytuivoice-"))

from textual.widgets import Static  # noqa: E402

import rockycode.engine.asr as asr_mod  # noqa: E402
import rockycode.engine.recorder as rec_mod  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.tui.app import ChatInput, RockyCodeApp  # noqa: E402

WD = Path.cwd()


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
    """One scripted model response per API call. Empty → 'ok.'"""
    def __init__(self):
        self.q = deque()
        self.calls = 0

    def text(self, s):
        self.q.append([chunk(content=s), chunk(usage=U())])
        return self

    async def create(self, **kwargs):
        self.calls += 1
        if not self.q:
            return stream([chunk(content="ok."), chunk(usage=U())])
        return stream(self.q.popleft())


class FakeRecorder:
    """Stands in for the ffmpeg recorder: start() writes a few bytes of PCM
    (enough to look non-empty), running is scripted, read_new feeds nothing
    (the VAD paths are covered in smoke_voice)."""

    stop_requested = False
    keep_running = False  # True → stays RECORDING until stop_requested

    def __init__(self):
        self._pcm = Path(tempfile.mkdtemp(prefix="fakevoice-")) / "voice.pcm"

    @property
    def running(self):
        return self.keep_running and not self.stop_requested

    async def start(self):
        self._pcm.write_bytes(b"\x00\x00" * 1600)  # 100 ms of silence
        return self._pcm

    def read_new(self):
        return b""

    async def stop(self):
        return self._pcm


def build(permission="ask", *, voice_engine="deepseek", voice_readback="off",
          voice_autosend="off", script=None):
    script = script or Script()
    registry = {}
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    engine = Engine(model="fake", client=client, workdir=WD, registry=registry)
    app = RockyCodeApp(
        engine, permission=permission,
        voice_engine=voice_engine, voice_readback=voice_readback,
        voice_autosend=voice_autosend,
    )
    return app, engine, script


def txt(app):
    return " ".join(str(w.render()) for w in app.query(Static))


async def wait_until(pilot, cond, timeout=6.0, step=0.05):
    for _ in range(int(timeout / step)):
        await pilot.pause(step)
        if cond():
            return True
    return bool(cond())


async def test_off_hints():
    app, _, _ = build(voice_engine="off")
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "🎙 off" in txt(app))
        await pilot.press("ctrl+g")
        ok = await wait_until(pilot, lambda: "voice_engine deepseek" in txt(app))
        assert ok, txt(app)
        assert app.query_one(ChatInput).text == ""  # never recorded
    print("  off: ctrl+g hints, records nothing; chip says off  ✓")


async def test_record_inserts():
    asr_mod.transcribe = _fake_transcribe
    rec_mod.Recorder = FakeRecorder
    app, _, _ = build()
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "ctrl+g" in txt(app))  # idle chip
        await pilot.press("ctrl+g")
        ok = await wait_until(pilot, lambda: app.query_one(ChatInput).text == "把测试跑一遍")
        assert ok, (txt(app), app.query_one(ChatInput).text)
        assert "heard" in txt(app)  # the muted receipt line
        assert "🎙" in txt(app) and "listening" not in txt(app).split("🎙")[0][-30:]
        chip = str(app.query_one("#voicechip", Static).render())
        assert "ctrl+g" in chip, chip  # back to idle
    print("  record: hotkey → (fake) ASR → transcript in the input box  ✓")


async def _fake_transcribe(wav, **_kw):
    return "把测试跑一遍", 1500.0


async def test_autosend_runs_a_turn():
    asr_mod.transcribe = _fake_transcribe
    rec_mod.Recorder = FakeRecorder
    script = Script().text("收到，开始干活。")
    app, _, _ = build(voice_autosend="on", script=script)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+g")
        ok = await wait_until(pilot, lambda: "收到，开始干活。" in txt(app))
        assert ok, txt(app)
        assert "把测试跑一遍" in txt(app)  # the user turn shows the transcript
        assert app.query_one(ChatInput).text == ""
    print("  autosend: the transcript is submitted — the engine answers  ✓")


async def test_barge_in_then_stop():
    asr_mod.transcribe = _fake_transcribe
    FakeRecorder.keep_running = True
    FakeRecorder.stop_requested = False
    rec_mod.Recorder = FakeRecorder
    app, _, _ = build()
    async with app.run_test() as pilot:
        app._voice.speaking()   # readback active (the speak loop drives this in real life)
        assert app._voice.phase == "speaking" and app._voice.audible
        await pilot.press("ctrl+g")            # BARGE: cut audio, start listening
        ok = await wait_until(pilot, lambda: app._voice.phase == "recording")
        assert ok, app._voice.phase
        assert not app._voice.audible, "the barge must cut the audio"
        assert app._recorder is not None       # and the mic is hot
        await pilot.press("ctrl+g")            # STOP: manual endpoint
        ok = await wait_until(pilot, lambda: app.query_one(ChatInput).text == "把测试跑一遍")
        assert ok, (txt(app), app.query_one(ChatInput).text)
    FakeRecorder.keep_running = False
    print("  barge: ctrl+g during speech cuts audio and records; again stops  ✓")


async def test_busy_while_transcribing():
    gate = asyncio.Event()

    async def gated(wav, **_kw):
        await gate.wait()
        return "把测试跑一遍", 1500.0

    asr_mod.transcribe = gated
    rec_mod.Recorder = FakeRecorder
    app, _, _ = build()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+g")
        ok = await wait_until(pilot, lambda: app._voice.phase == "transcribing")
        assert ok, app._voice.phase
        await pilot.press("ctrl+g")            # busy — one clip at a time
        ok = await wait_until(pilot, lambda: "still transcribing" in txt(app))
        assert ok, txt(app)
        gate.set()
        ok = await wait_until(pilot, lambda: app.query_one(ChatInput).text == "把测试跑一遍")
        assert ok, app.query_one(ChatInput).text
    print("  busy: a second press while transcribing is ignored with a line  ✓")


async def test_readback_no_key():
    # A reply with readback on but no key: the reply still renders (the
    # transcript keeps the text), one permanent muted line says why, and
    # readback is off for the session — no retry per sentence.
    script = Script().text("你好，我是 rocky。")
    app, _, _ = build(voice_readback="on", script=script)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: "ctrl+g" in txt(app))
        await pilot.press(*"说句话")
        await pilot.press("enter")
        ok = await wait_until(pilot, lambda: "no DeepSeek API key" in txt(app))
        assert ok, txt(app)
        assert "你好，我是 rocky。" in txt(app)  # the reply itself rendered
        assert app._voice_failed                  # and readback stays silent
    print("  readback: no key → one permanent line; the reply still renders  ✓")


def run():
    asyncio.run(test_off_hints())
    asyncio.run(test_record_inserts())
    asyncio.run(test_autosend_runs_a_turn())
    asyncio.run(test_barge_in_then_stop())
    asyncio.run(test_busy_while_transcribing())
    asyncio.run(test_readback_no_key())


if __name__ == "__main__":
    run()
    print("TUI VOICE SMOKE OK — off-hint · record-insert · autosend · barge · busy · readback. amaze!")
