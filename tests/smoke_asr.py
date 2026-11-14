"""DeepSeek ASR client — request shape, error mapping, key gating. Hermetic:
httpx.MockTransport, no key, no network (the no-key paths raise before any
socket is touched).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rockyhome-")
os.environ.pop("ROCKYCODE_API_KEY", None)  # hermetic: no ambient key

import httpx  # noqa: E402

from rockycode.engine.asr import (  # noqa: E402
    ASR_URL,
    AsrError,
    home_key,
    transcribe,
    transcribe_path_sync,
)

WAV = b"RIFF" + b"\x00" * 100  # the transport never parses it


def test_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["ctype"] = request.headers.get("content-type")
        body = request.read()
        seen["file"] = b'filename="voice.wav"' in body
        seen["wav"] = WAV in body
        return httpx.Response(200, json={"text": " 上海今天天气怎么样？ ", "duration_ms": 3200})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text, dur = await transcribe(WAV, key="test-key", client=client)
        assert text == "上海今天天气怎么样？", text
        assert dur == 3200.0
        assert seen["url"] == ASR_URL
        assert seen["auth"] == "Bearer test-key"
        assert (seen["ctype"] or "").startswith("multipart/form-data")
        assert seen["file"] and seen["wav"], seen

    asyncio.run(run())
    print("  request: multipart file + bearer auth + transcript trim  ✓")


def test_error_mapping():
    cases = {
        400: "WAV",
        408: "too long",
        413: "8 MiB",
        429: "rate-limited",
        503: "busy",
        504: "timed out",
    }

    async def run():
        for status, expect in cases.items():
            def handler(request: httpx.Request, _s=status) -> httpx.Response:
                return httpx.Response(_s, json={})
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await transcribe(WAV, key="k", client=client)
                raise AssertionError(f"{status} did not raise")
            except AsrError as e:
                assert expect in str(e), (status, str(e))
        # network-level failures carry a retryable message, not a stack trace
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")
        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        try:
            await transcribe(WAV, key="k", client=client)
            raise AssertionError("network failure did not raise")
        except AsrError as e:
            assert "network" in str(e)
        # an empty transcript is an error, not silence
        def empty(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"text": "", "duration_ms": 100})
        client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        try:
            await transcribe(WAV, key="k", client=client)
            raise AssertionError("empty transcript did not raise")
        except AsrError as e:
            assert "silent" in str(e)

    asyncio.run(run())
    print("  errors: 400/408/413/429/503/504 → one-line fixes; empty = silent  ✓")


def test_key_gating():
    # No key anywhere → the error names the env var and no request is made.
    async def run():
        key, src = home_key()
        assert key is None and "ROCKYCODE_API_KEY" in src, src
        try:
            await transcribe(WAV, key="")
            raise AssertionError("no-key call did not raise")
        except AsrError as e:
            assert "ROCKYCODE_API_KEY" in str(e)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(WAV)
            path = Path(f.name)
        try:
            transcribe_path_sync(path)
            raise AssertionError("no-key sync call did not raise")
        except AsrError as e:
            assert "ROCKYCODE_API_KEY" in str(e)
        path.unlink()
        # An explicit key bypasses env lookup entirely (the mock never runs).
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"text": "hi", "duration_ms": 1})))
        text, _ = await transcribe(WAV, key="explicit", client=client)
        assert text == "hi"

    asyncio.run(run())
    print("  key: missing → env-var error; explicit key bypasses env  ✓")


test_request_shape()
test_error_mapping()
test_key_gating()
print("ASR SMOKE OK — shape · errors · key gating. amaze!")
