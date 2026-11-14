"""DeepSeek TTS + local playback — request shape, error classes, the player's
barge-in contract, and the recorder's platform args. Hermetic: MockTransport
for the API, a `sleep` subprocess as the "audio device" (no sound, no ffmpeg
needed to test stop()).
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.environ.pop("ROCKYCODE_API_KEY", None)  # hermetic: no ambient key

import httpx  # noqa: E402

from rockycode.engine.recorder import ffmpeg_available, ffmpeg_input_args  # noqa: E402
from rockycode.engine.tts import Player, TtsError, pick_player, synthesize  # noqa: E402


def test_synthesize_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen["json"] = httpx.Response(200, json=json_of(body)).json()
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"RIFF" + b"\x00" * 60,
                              headers={"content-type": "audio/wav"})

    def json_of(body: bytes) -> dict:
        import json as _json
        return _json.loads(body.decode())

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = await synthesize("你好，我是 rocky。", key="k", client=client)
        assert data.startswith(b"RIFF")
        assert seen["json"] == {"text": "你好，我是 rocky。", "voice_id": "stella"}
        assert seen["auth"] == "Bearer k"
        # an unknown voice_id falls back to stella, never to an API error
        await synthesize("hi", key="k", voice_id="pink", client=client)
        assert seen["json"]["voice_id"] == "stella"

    asyncio.run(run())
    print("  synth: JSON body + voice fallback + wav bytes  ✓")


def test_synthesize_errors():
    async def run():
        def busy(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})
        client = httpx.AsyncClient(transport=httpx.MockTransport(busy))
        try:
            await synthesize("x", key="k", client=client)
            raise AssertionError("503 did not raise")
        except TtsError as e:
            assert "busy" in str(e) and not e.permanent  # retryable — next turn
        def empty(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")
        client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        try:
            await synthesize("x", key="k", client=client)
            raise AssertionError("empty clip did not raise")
        except TtsError as e:
            assert "empty" in str(e)
        # no key → permanent (retrying will never help)
        try:
            await synthesize("x", key="")
            raise AssertionError("no-key call did not raise")
        except TtsError as e:
            assert "ROCKYCODE_API_KEY" in str(e) and e.permanent

    asyncio.run(run())
    print("  errors: transient (503) vs permanent (no key) · empty clip  ✓")


def test_player_barge_in():
    # `sh -c 'sleep 5' <wav>` stands in for afplay/ffplay: speak() returns,
    # playing is True, stop() kills it fast — the whole barge-in contract.
    p = Player(argv=("sh", "-c", "sleep 5"))
    assert p.available()
    assert p.speak(b"RIFF" + b"\x00" * 44)
    assert p.playing
    t0 = time.monotonic()
    p.stop()
    assert not p.playing
    assert time.monotonic() - t0 < 2.0, "stop() must kill instantly, not wait out the clip"
    p.stop()  # idempotent
    print("  player: speak → playing; stop() kills instantly; idempotent  ✓")


def test_player_missing():
    p = Player(argv=None)  # auto-pick — hint text is independent of what's installed
    assert "afplay" in p.missing_hint() and "ffplay" in p.missing_hint()
    ghost = Player(argv=("no-such-binary-xyz",))  # spawn fails → False, no crash
    assert not ghost.speak(b"RIFF")
    assert not ghost.playing
    assert pick_player() is None or isinstance(pick_player(), tuple)
    print("  player: no binary → honest hint, failed spawn is a clean no-op  ✓")


def test_recorder_args():
    assert ffmpeg_input_args("Darwin") == ("-f", "avfoundation", "-i", ":default")
    assert ffmpeg_input_args("Linux") == ("-f", "pulse", "-i", "default")
    assert ffmpeg_input_args("Windows") == ("-f", "dshow", "-i", "audio=default")
    assert ffmpeg_input_args("Haiku") == ()  # unknown platform → honest empty
    assert ffmpeg_available() is None or isinstance(ffmpeg_available(), str)
    print("  recorder: per-platform input args; availability is a hint or None  ✓")


test_synthesize_shape()
test_synthesize_errors()
test_player_barge_in()
test_player_missing()
test_recorder_args()
print("TTS SMOKE OK — shape · errors · barge-in player · recorder args. amaze!")
