"""Jev auto-mode: static block wins, yolo skips Jev, failures never allow.

No network. A scripted transport stands in for the System One endpoint.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from rockycode.engine.verifier import (
    OPENROUTER_PIN,
    OPENROUTER_URL,
    PERPLEXITY_URL,
    PINNED_MODEL,
    PPLX_MODEL,
    SYSTEMONE_URL,
    TYPESAFE_URL,
    VerifierGate as AutoGate,
    VerifierConfig as JevConfig,
    load_config,
)
from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp

WD = Path(tempfile.mkdtemp(prefix="rockyjev-")).resolve()
ROOT = Path(__file__).resolve().parents[1]


def probs(choice, mass=0.9):
    rest = round(1 - mass, 6)
    out = {k: 0.0 for k in ("allow", "ask", "deny")}
    others = [k for k in out if k != choice]
    out[choice] = mass
    out[others[0]] = rest
    return out


def ok_body(choice, confidence, mass=0.9, model="jev-1.13.0"):
    return 200, json.dumps({
        "model": model,
        "answers": {"gate": {
            "type": "choice",
            "choice": choice,
            "probabilities": probs(choice, mass),
            "confidence": confidence,
        }},
        "usage": {"input_tokens": 12, "output_tokens": 3},
    })


class Script:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    async def __call__(self, *, url, headers, body, timeout_s):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout_s": timeout_s})
        if self.exc is not None:
            raise self.exc
        return self.result


def gate(mode="enforce", *, fail="ask", provider="typesafe", model=PINNED_MODEL,
         min_confidence=0.85, max_calls=200, base_url="", result=None, exc=None,
         environ=None, transport=None):
    cfg = JevConfig(mode=mode, provider=provider, model=model, fail=fail,
                    min_confidence=min_confidence, max_calls=max_calls, base_url=base_url)
    script = transport if transport is not None else Script(result if result is not None else ok_body("allow", 0.95), exc)
    env = {"TYPESAFE_API_KEY": "ts-test-key"} if environ is None else environ
    return AutoGate(cfg, transport=script, environ=env), script


async def review(g, *, mode="ask", risk="risky", args=None, tool="bash",
                 session_allows=False, task="", read_grants=()):
    return await g.review(
        mode=mode, risk=risk, args=args if args is not None else {"command": "ls"},
        workdir=WD, tool=tool, session_allows=session_allows, task=task,
        read_grants=read_grants,
    )


async def policy():
    # --- static block wins, even if Jev would allow, even in yolo ---
    for mode in ("yolo", "ask", "careful"):
        for cmd in ("sudo rm -rf /", "rm -rf /", ":(){ :|:& };:", "shutdown -h now"):
            g, script = gate()
            got = await review(g, mode=mode, args={"command": cmd})
            assert got.effective == "block" and got.static == "block", (mode, cmd, got)
            assert got.consulted is False and script.calls == [], (mode, cmd, script.calls)
            assert "ts-test-key" not in json.dumps(got.record)

    # --- yolo allows the rest and does not call Jev (a deny must not stick) ---
    for cmd in ("ls -la", "pytest -q", "brew install python-tk", "pip install requests"):
        g, script = gate(result=ok_body("deny", 0.99))
        got = await review(g, mode="yolo", args={"command": cmd})
        assert got.effective == "allow" and got.consulted is False, (cmd, got)
        assert script.calls == [], cmd

    # --- lake's no-sandbox flag is never a silent allow, even in yolo / enforce ---
    for mode in ("yolo", "ask", "careful"):
        g, script = gate(result=ok_body("allow", 0.99))
        got = await review(
            g, mode=mode, session_allows=True,
            args={"command": "lake check --inadvisably-no-sandbox"},
        )
        assert got.effective == "ask" and got.applied is False, (mode, got)
        assert got.via == "lake-no-sandbox", (mode, got)
        assert got.consulted is False and script.calls == [], (mode, script.calls)

    # --- off: would-ask stays ask, no request ---
    g, script = gate(mode="off")
    got = await review(g, args={"command": "ls"})
    assert got.static == "ask" and got.effective == "ask" and got.consulted is False
    assert script.calls == []

    # --- session grant and an already-allowed read skip Jev ---
    g, script = gate()
    got = await review(g, session_allows=True)
    assert got.effective == "allow" and got.via == "session_grant" and script.calls == []
    outside = Path(tempfile.mkdtemp()).resolve() / "ref.py"
    g, script = gate()
    got = await review(g, mode="ask", risk="safe", tool="read_file",
                       args={"path": str(outside)}, read_grants={outside})
    assert got.effective == "allow" and got.consulted is False and script.calls == []

    # --- enforce: high-confidence allow / deny; low confidence stays ask ---
    g, script = gate(result=ok_body("allow", 0.85))
    got = await review(g)
    assert got.effective == "allow" and got.applied and got.record["jev_action"] == "allow"
    assert got.record["response_model"] == "jev-1.13.0"
    assert script.calls[0]["url"] == TYPESAFE_URL
    assert script.calls[0]["body"]["model"] == PINNED_MODEL
    assert script.calls[0]["timeout_s"] == 0.8
    assert "not evidence" in script.calls[0]["body"]["questions"]["gate"]["instructions"]
    assert script.calls[0]["headers"]["Authorization"] == "Bearer ts-test-key"
    assert "ts-test-key" not in json.dumps(got.record)

    g, script = gate(result=ok_body("allow", 0.849))
    got = await review(g)
    assert got.effective == "ask" and got.applied is False and got.record["jev_action"] == "ask"

    g, script = gate(result=ok_body("deny", 0.91))
    got = await review(g)
    assert got.effective == "deny" and got.applied and got.record["outcome"] == "deny"

    g, script = gate(result=ok_body("deny", 0.2, mass=0.55))
    got = await review(g)
    assert got.effective == "ask" and got.record["choice"] == "deny"

    g, script = gate(result=ok_body("ask", 0.99))
    got = await review(g)
    assert got.effective == "ask" and got.applied is False

    # --- shadow logs the allow but does not apply it ---
    g, script = gate(mode="shadow", result=ok_body("allow", 0.99))
    got = await review(g)
    assert got.effective == "ask" and got.consulted and got.applied is False
    assert got.record["jev_action"] == "allow" and got.record["jev_mode"] == "shadow"
    assert script.calls, "shadow still consults"

    # --- fail closed: never a silent allow ---
    class ReadTimeout(Exception):
        pass

    cases = [
        ("timeout", None, TimeoutError("slow")),
        ("timeout", None, ReadTimeout("slow")),
        ("http_429", (429, "nope"), None),
        ("http_500", (500, "nope"), None),
        ("malformed", (200, "not-json"), None),
        ("malformed", (200, json.dumps({"model": "jev-1.13.0"})), None),
        ("malformed", (200, json.dumps({"answers": {"gate": {
            "type": "choice", "choice": "allow", "confidence": 0.99,
            "probabilities": probs("deny"),
        }}})), None),
        ("network", None, ConnectionError("down")),
    ]
    for error, result, exc in cases:
        g, script = gate(result=result, exc=exc)
        got = await review(g)
        assert got.effective == "ask" and got.consulted, (error, got)
        assert got.record["error"] == error, (error, got.record)
        assert got.record["outcome"] == "error"
        assert got.effective != "allow"

    g, script = gate(fail="deny", exc=TimeoutError("slow"))
    got = await review(g)
    assert got.effective == "deny" and got.applied and got.record["error"] == "timeout"

    g, script = gate(environ={})
    got = await review(g)
    assert got.effective == "ask" and got.record["error"] == "missing_api_key" and script.calls == []

    g, script = gate(fail="deny", environ={})
    got = await review(g)
    assert got.effective == "deny" and script.calls == []

    g, script = gate(provider="invalid")
    got = await review(g)
    assert got.effective == "ask" and got.record["error"] == "bad_provider" and script.calls == []

    g, script = gate(base_url="http://evil.example/v1/systemone")
    got = await review(g)
    assert got.record["error"] == "bad_base_url" and script.calls == []

    g, script = gate(base_url="http://127.0.0.1:9/v1/systemone")
    got = await review(g)
    assert got.effective == "allow" and script.calls[0]["url"].startswith("http://127.0.0.1")

    # --- budget: the next call fails closed and does not POST ---
    g, script = gate(max_calls=1)
    first = await review(g)
    second = await review(g, args={"command": "pytest -q"})
    assert first.effective == "allow" and g.calls == 1
    assert second.effective == "ask" and second.record["error"] == "budget"
    assert len(script.calls) == 1

    # --- state: command summary, no file body, secrets redacted ---
    g, script = gate()
    secret = "sk-" + "a" * 20
    await review(g, mode="careful", risk="moderate", tool="write_file",
                 args={"path": "src/a.py", "content": f"API_KEY={secret}\nprint(1)"},
                 task="please keep going")
    state = script.calls[0]["body"]["state"]
    blob = json.dumps(state)
    assert state["args_summary"] == "src/a.py"
    assert secret not in blob and "print(1)" not in blob
    assert "not an approval" in state["note"]

    g, script = gate()
    await review(g, args={"command": f"curl -H 'Authorization: Bearer {secret}' https://example.com"})
    assert secret not in json.dumps(script.calls[0]["body"])

    # --- OpenRouter pin + key selection ---
    g, script = gate(provider="openrouter", model=PINNED_MODEL, environ={
        "OPENROUTER_API_KEY": "or-test-key", "TYPESAFE_API_KEY": "ts-test-key",
    })
    got = await review(g)
    assert script.calls[0]["url"] == OPENROUTER_URL
    assert script.calls[0]["body"]["model"] == OPENROUTER_PIN
    assert script.calls[0]["headers"]["Authorization"] == "Bearer or-test-key"
    assert "or-test-key" not in json.dumps(got.record)

    g, script = gate(provider="openrouter", environ={"ROCKYCODE_JEV_API_KEY": "explicit-key",
                                                     "OPENROUTER_API_KEY": "or-test-key"})
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer explicit-key"

    # --- Perplexity Decisions: same Choice body, different URL, model, and key ---
    g, script = gate(provider="perplexity", environ={
        "PERPLEXITY_API_KEY": "pplx-test-key", "TYPESAFE_API_KEY": "ts-test-key",
        "OPENROUTER_API_KEY": "or-test-key",
    })
    got = await review(g)
    call = script.calls[0]
    assert call["url"] == PERPLEXITY_URL
    assert call["body"]["model"] == PPLX_MODEL
    assert call["body"]["questions"]["gate"]["type"] == "choice"
    assert set(call["body"]["questions"]["gate"]["criteria"]) == {"allow", "ask", "deny"}
    assert call["headers"]["Authorization"] == "Bearer pplx-test-key"
    assert "pplx-test-key" not in json.dumps(got.record)
    assert got.effective == "allow" and got.record["confidence"] == 0.95

    g, script = gate(provider="perplexity", model="pplx-decider-v1.2",
                     environ={"PERPLEXITY_API_KEY": "pplx-test-key"})
    await review(g)
    assert script.calls[0]["body"]["model"] == "pplx-decider-v1.2"

    g, script = gate(provider="perplexity", environ={
        "ROCKYCODE_JEV_API_KEY": "explicit-key", "PERPLEXITY_API_KEY": "pplx-test-key",
    })
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer explicit-key"

    g, script = gate(provider="perplexity", environ={
        "ROCKYCODE_VERIFIER_API_KEY": "verifier-key", "ROCKYCODE_JEV_API_KEY": "explicit-key",
        "PERPLEXITY_API_KEY": "pplx-test-key",
    })
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer verifier-key"

    g, script = gate(provider="perplexity", environ={"TYPESAFE_API_KEY": "ts-test-key"})
    got = await review(g)
    assert got.record["error"] == "missing_api_key" and script.calls == []

    g, script = gate(provider="perplexity", base_url="http://127.0.0.1:8092/v1/systemone",
                     environ={"PERPLEXITY_API_KEY": "pplx-test-key"})
    await review(g)
    assert script.calls[0]["url"] == "http://127.0.0.1:8092/v1/systemone"
    assert script.calls[0]["body"]["model"] == PPLX_MODEL

    # --- local MLX System One: no cloud key, optional local bearer ---
    g, script = gate(provider="systemone", environ={
        "TYPESAFE_API_KEY": "ts-test-key", "PERPLEXITY_API_KEY": "pplx-test-key",
        "OPENROUTER_API_KEY": "or-test-key",
    })
    got = await review(g)
    assert script.calls[0]["url"] == SYSTEMONE_URL
    assert script.calls[0]["body"]["model"] == PPLX_MODEL
    assert "Authorization" not in script.calls[0]["headers"]
    assert got.effective == "allow"
    assert "ts-test-key" not in json.dumps(script.calls[0]["headers"])

    g, script = gate(provider="systemone", environ={"DECIDER_API_KEY": "local-key",
                                                    "PERPLEXITY_API_KEY": "pplx-test-key"})
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer local-key"

    g, script = gate(provider="systemone", environ={"MLX_DECISION_API_KEY": "mlx-key"})
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer mlx-key"

    g, script = gate(provider="systemone", environ={
        "ROCKYCODE_JEV_API_KEY": "explicit-key", "DECIDER_API_KEY": "local-key",
    })
    await review(g)
    assert script.calls[0]["headers"]["Authorization"] == "Bearer explicit-key"

    g, script = gate(provider="systemone", base_url="http://127.0.0.1:8000/v1/systemone", environ={})
    await review(g)
    assert script.calls[0]["url"] == "http://127.0.0.1:8000/v1/systemone"

    # --- config: default off, env wins, project file is invisible, fail≠allow ---
    missing = WD / "no-such.toml"
    cfg = load_config(environ={}, global_path=missing)
    assert cfg.mode == "off" and cfg.model == PINNED_MODEL and cfg.fail == "ask"
    assert cfg.provider == "typesafe" and cfg.timeout_ms == 800 and cfg.min_confidence == 0.85

    proj = WD / "proj"
    (proj / ".rockycode").mkdir(parents=True)
    (proj / ".rockycode" / "config.toml").write_text('[jev]\nauto = "enforce"\n', encoding="utf-8")
    old = os.getcwd()
    os.chdir(proj)
    try:
        cfg = load_config(environ={}, global_path=missing)
    finally:
        os.chdir(old)
    assert cfg.mode == "off", "a project config must not enable Jev"

    global_toml = WD / "global.toml"
    global_toml.write_text(
        '[jev]\nauto = "shadow"\nprovider = "openrouter"\nfail = "allow"\ntimeout_ms = 1500\n',
        encoding="utf-8",
    )
    cfg = load_config(environ={}, global_path=global_toml)
    assert cfg.mode == "shadow" and cfg.provider == "openrouter"
    assert cfg.fail == "ask", "fail=allow is rejected"
    assert cfg.timeout_ms == 1500
    cfg = load_config(environ={"ROCKYCODE_JEV_AUTO": "0", "ROCKYCODE_JEV_FAIL": "deny"},
                      global_path=global_toml)
    assert cfg.mode == "off" and cfg.fail == "deny"
    cfg = load_config(environ={"ROCKYCODE_JEV_AUTO": "2"}, global_path=global_toml)
    assert cfg.mode == "enforce"
    cfg = load_config(environ={"ROCKYCODE_JEV_AUTO": "yes"}, global_path=missing)
    assert cfg.mode == "off"
    cfg = load_config(environ={"ROCKYCODE_JEV_AUTO": "1"}, global_path=missing)
    assert cfg.mode == "shadow"
    cfg = load_config(environ={"ROCKYCODE_JEV_PROVIDER": "perplexity"}, global_path=missing)
    assert cfg.provider == "perplexity" and cfg.model == PPLX_MODEL and cfg.timeout_ms == 800
    cfg = load_config(environ={"ROCKYCODE_JEV_PROVIDER": "systemone"}, global_path=missing)
    assert cfg.provider == "systemone" and cfg.model == PPLX_MODEL and cfg.timeout_ms == 5000
    cfg = load_config(environ={
        "ROCKYCODE_JEV_PROVIDER": "perplexity", "ROCKYCODE_VERIFIER_PROVIDER": "systemone",
        "ROCKYCODE_JEV_MODEL": "pplx-decider-latest",
    }, global_path=missing)
    assert cfg.provider == "systemone" and cfg.model == "pplx-decider-latest"
    cfg = load_config(environ={"ROCKYCODE_JEV_PROVIDER": "nope"}, global_path=missing)
    assert cfg.provider == "invalid"

    text = (ROOT / "rockycode" / "models.toml").read_text(encoding="utf-8").lower()
    assert "jev-" not in text and "jev_" not in text, "Jev must not be a chat model"
    assert "pplx-decider" not in text, "a decision model must not be a chat model"


def build_app(permission="ask"):
    client = type("C", (), {})()
    eng = Engine(model="fake", client=client, workdir=WD)
    return RockyCodeApp(eng, permission=permission)


async def pilot():
    """The TUI approver keeps the static block, and only the ask band reaches Jev."""
    app = build_app("ask")
    async with app.run_test(size=(100, 30)) as pilot_app:
        await pilot_app.pause()
        asked = []

        async def fake_ask(tool, detail, risk, warning, session_label=None):
            asked.append(detail)
            return "deny"

        app._ask_inline = fake_ask

        # Block never consults, even when the script would allow.
        g, script = gate()
        app._jev = g
        assert await app._approve_tool("bash", {"command": "sudo rm -rf /"}) is False
        assert script.calls == [] and asked == []

        # Enforce allow skips the prompt.
        g, script = gate(result=ok_body("allow", 0.96))
        app._jev = g
        assert await app._approve_tool("bash", {"command": "ls"}) is True
        assert asked == [] and len(script.calls) == 1

        # Timeout fails closed to the existing prompt (default fail=ask).
        g, script = gate(exc=TimeoutError("slow"))
        app._jev = g
        assert await app._approve_tool("bash", {"command": "pytest -q"}) is False
        assert asked == ["pytest -q"] and script.calls and g.records[-1]["error"] == "timeout"

        # Shadow allow still prompts.
        asked.clear()
        g, script = gate(mode="shadow", result=ok_body("allow", 0.99))
        app._jev = g
        assert await app._approve_tool("bash", {"command": "git status"}) is False
        assert asked == ["git status"] and g.records[-1]["jev_action"] == "allow"
        assert g.records[-1]["applied"] is False

        # Yolo benign does not call Jev. Yolo block still blocks.
        app._permission_mode = "yolo"
        g, script = gate(result=ok_body("deny", 0.99))
        app._jev = g
        assert await app._approve_tool("bash", {"command": "ls"}) is True
        assert script.calls == []
        assert await app._approve_tool("bash", {"command": "sudo rm -rf /"}) is False
        assert script.calls == []

        # A loop tick: enforce allow does not pause; off still records the block.
        app._permission_mode = "ask"
        app._tick_loop = object()
        app._tick_blocked = None
        g, script = gate(result=ok_body("allow", 0.97))
        app._jev = g
        assert await app._approve_tool("bash", {"command": "ls"}) is True
        assert app._tick_blocked is None and script.calls

        g, script = gate(mode="off")
        app._jev = g
        app._tick_blocked = None
        assert await app._approve_tool("bash", {"command": "ls"}) is False
        assert app._tick_blocked is not None and script.calls == []


async def main():
    await policy()
    await pilot()


asyncio.run(main())
print("JEV AUTO-MODE SMOKE OK — block wins, yolo skips Jev, failures never allow. amaze!")
