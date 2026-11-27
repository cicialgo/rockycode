"""MCP tool results flagged isError are capped like every other tool output."""
from types import SimpleNamespace

from rockycode.engine.mcp import _result_text
from rockycode.engine.tools import MAX_OUTPUT_CHARS


def main():
    huge = SimpleNamespace(content=[SimpleNamespace(text="E" * 100_000)], isError=True)
    err = _result_text(huge)
    assert err.startswith("[error] "), err[:20]
    assert "chars truncated" in err
    assert len(err) <= MAX_OUTPUT_CHARS + 100, len(err)

    ok = _result_text(SimpleNamespace(content=[SimpleNamespace(text="E" * 100_000)], isError=False))
    assert "chars truncated" in ok
    assert len(ok) <= MAX_OUTPUT_CHARS + 100, len(ok)
    assert not ok.startswith("[error] ")

    short = _result_text(SimpleNamespace(content=[SimpleNamespace(text="bad arg")], isError=True))
    assert short == "[error] bad arg", short
    print("isError tool results truncated like normal output  ✓")
    print("MCP ERROR TRUNCATION SMOKE OK. amaze!")


if __name__ == "__main__":
    main()
