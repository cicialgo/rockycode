"""A failed model request is one plain sentence with the next step.

Before: `✗ AuthenticationError: Error code: 401 - {'error': {…}} · key source: …`
— a class name and a dict. Now the engine says what kind of failure it was
(engine/errors.py), each front end words it (the TUI in rocky's voice and the
session's language), and the SDK's own text rides along as a dim second line.

Real SDK exception classes, no network.
"""
import asyncio
import os
import re
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
home = Path(tempfile.mkdtemp(prefix="rocky-errors-"))
os.environ["ROCKYCODE_HOME"] = str(home)
os.environ["ROCKYCODE_OLLAMA_URL"] = "http://127.0.0.1:9"
for _name in [n for n in os.environ if re.fullmatch(r"ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL", n)]:
    os.environ.pop(_name)
os.environ["ROCKYCODE_API_KEY"] = "k-home-fixture-value"
os.chdir(tempfile.mkdtemp(prefix="rocky-errors-work-"))

import httpx  # noqa: E402
import openai  # noqa: E402

from rockycode import onboarding  # noqa: E402

onboarding._keyring_key = lambda: None

from rockycode.engine.errors import KINDS, explain  # noqa: E402
from rockycode.engine.events import EngineError  # noqa: E402
from rockycode.engine.loop import Engine  # noqa: E402
from rockycode.headless import event_to_line  # noqa: E402
from rockycode.i18n import PALETTE, tr  # noqa: E402
from rockycode.server import _event_to_notification  # noqa: E402
from rockycode.tui.app import RockyCodeApp  # noqa: E402
from rockycode.tui.chatinput import ChatInput  # noqa: E402

REQUEST = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


def status_error(cls, status, message, body=None):
    return cls(f"Error code: {status} - {body or {'error': {'message': message}}}",
               response=httpx.Response(status, request=REQUEST), body=body or {"error": {"message": message}})


CASES = [
    (status_error(openai.AuthenticationError, 401, "Authentication Fails, Your api key is invalid"), "auth"),
    (status_error(openai.PermissionDeniedError, 403, "forbidden"), "auth"),
    (status_error(openai.APIStatusError, 402, "Insufficient Balance"), "credit"),
    (status_error(openai.RateLimitError, 429, "You exceeded your current quota", {"error": {"code": "insufficient_quota"}}), "credit"),
    (status_error(openai.RateLimitError, 429, "Rate limit reached"), "rate_limit"),
    (openai.APITimeoutError(request=REQUEST), "timeout"),
    (openai.APIConnectionError(request=REQUEST), "network"),
    (status_error(openai.BadRequestError, 400, "This model's maximum context length is 1048576 tokens"), "context"),
    (status_error(openai.NotFoundError, 404, "Not Found"), "model"),
    (status_error(openai.BadRequestError, 400, "Model Not Exist"), "model"),
    (status_error(openai.BadRequestError, 400, "max_tokens exceeds the maximum of 8192"), "request"),
    (status_error(openai.InternalServerError, 503, "overloaded"), "server"),
    (ValueError("something else broke"), "unknown"),
]
for error, kind in CASES:
    said = explain(error, model="deepseek-flash", host="api.deepseek.com", key_source="ROCKYCODE_API_KEY → https://api.deepseek.com/v1")
    assert said.kind == kind and kind in KINDS, (type(error).__name__, str(error)[:60], said.kind, kind)
    assert said.message.endswith(".") or kind == "unknown", said.message
    assert "{'error'" not in said.message or kind == "unknown", "the sentence never quotes the provider's body"
    if kind in ("auth", "credit", "request"):
        assert "key source: ROCKYCODE_API_KEY → https://api.deepseek.com/v1" in said.message, said.message
    else:
        assert "key source" not in said.message, "a full context window is not a key problem"
print(f"explain: {len(CASES)} failures sorted into kinds; the key's source is named where the key may be the cause  ✓")


def failing(error):
    async def create(**kwargs):
        raise error
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


async def one_error(engine) -> EngineError:
    async for ev in engine.run_turn("hi"):
        if isinstance(ev, EngineError):
            return ev
    raise AssertionError("no error event")


async def main():
    # ── the engine's event ───────────────────────────────────────────────────
    auth = CASES[0][0]
    ev = await one_error(Engine(model="deepseek-flash", client=failing(auth), workdir=Path.cwd()))
    assert ev.kind == "auth" and ev.message.startswith("The key was rejected (401)"), ev.message
    assert "key source: ROCKYCODE_API_KEY → https://api.deepseek.com/v1" in ev.message
    assert ev.detail.startswith("AuthenticationError: Error code: 401"), ev.detail
    assert ev.params["status"] == 401 and ev.params["host"] == "api.deepseek.com" and ev.params["model"] == "deepseek-flash"
    assert "k-home-fixture-value" not in ev.message + ev.detail + str(ev.params), "never the key"
    odd = await one_error(Engine(model="deepseek-flash", client=failing(ValueError("something else broke")), workdir=Path.cwd()))
    assert (odd.kind, odd.message, odd.detail) == ("unknown", "ValueError: something else broke", "")
    print("engine: the event carries the sentence, the kind, the facts and the SDK's own text  ✓")

    # ── exec and serve pass the kind along ───────────────────────────────────
    assert event_to_line(ev) == {"type": "error", "message": ev.message, "kind": "auth"}
    assert "kind" not in event_to_line(EngineError(message="step limit (3) reached — stopping this turn."))
    note = _event_to_notification("s1", ev)["params"]
    assert note["kind"] == "auth" and note["detail"].startswith("AuthenticationError") and note["message"] == ev.message
    print("hosts: exec's JSONL and serve's notification carry `kind` for a failed request  ✓")

    # ── the TUI words it ─────────────────────────────────────────────────────
    for error, kind, said in ((auth, "auth", "the key was rejected (401)"),
                              (CASES[5][0], "timeout", "api.deepseek.com did not answer in time"),
                              (CASES[7][0], "context", "this conversation is too long for deepseek-flash")):
        engine = Engine(model="deepseek-flash", client=failing(error), workdir=Path.cwd())
        engine.evolve_enabled = False
        app = RockyCodeApp(engine, permission="yolo")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            box = app.query_one(ChatInput)
            box.focus()
            box.text = "hi"
            await pilot.press("enter")
            for _ in range(80):
                await pilot.pause(0.05)
                lines = [str(w.render()) for w in app.query(".error-msg")]
                if lines:
                    break
            assert len(lines) == 1 and lines[0].startswith("✗ " + said), lines
            assert type(error).__name__ in lines[0], "the SDK's text is still there, on the second line"
            assert "k-home-fixture-value" not in lines[0]
            if kind == "auth":
                assert "paste a new one: /model → add or change a key" in lines[0] and "key source: ROCKYCODE_API_KEY" in lines[0], lines[0]
            if kind == "context":
                assert "/task new <note>" in lines[0] and "key source" not in lines[0].splitlines()[0]
    facts = {"status": 401, "host": "h", "model": "m", "key_source": "K → U"}
    for kind in KINDS[:-1]:
        en, zh = tr(f"errors.{kind}", "en", **PALETTE, **facts), tr(f"errors.{kind}", "zh", **PALETTE, **facts)
        assert en != f"errors.{kind}" and zh != en and "{" not in en + zh, (kind, en, zh)
    print("tui: rocky says what happened and the next step, in the session's language; the raw line is dimmed  ✓")


asyncio.run(main())
print("ERRORS SMOKE OK — when it breaks, rocky says what to do. amaze!")
