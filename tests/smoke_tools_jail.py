"""Path-confinement (jail) smoke test for the LOCAL file tools. No Docker.

read_file / write_file / edit_file must refuse any target outside workdir —
absolute paths, `..` escapes, and symlinks that point out — on their own, not
relying on the advisory permission layer (bench/serve/yolo bypass it). And
write/edit must refuse secret files symmetrically with read.
"""
import asyncio
import os
import tempfile
from pathlib import Path

from rockycode.engine import tools as tools_mod


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="rockyjail-")).resolve()
    outside = Path(tempfile.mkdtemp(prefix="rockyout-")).resolve()
    (outside / "secret.txt").write_text("TOP SECRET\n")
    (workdir / "ok.txt").write_text("hello\n")

    reg = tools_mod.build_registry(workdir)

    async def call(tool, args):
        return await tools_mod.execute(reg, tool, args)

    import json as _json
    def a(**kw):
        return _json.dumps(kw)

    # --- in-workdir operations still work ---
    out, ok = await call("read_file", a(path="ok.txt"))
    assert ok and "hello" in out, out
    out, ok = await call("write_file", a(path="sub/new.txt", content="x"))
    assert ok and "[ok]" in out, out
    assert (workdir / "sub" / "new.txt").read_text() == "x"
    out, ok = await call("edit_file", a(path="ok.txt", old_string="hello", new_string="bye"))
    assert ok and "[ok]" in out, out
    assert (workdir / "ok.txt").read_text() == "bye\n"
    print("jail: in-workdir read/write/edit still work  ✓")

    # `[blocked]` is the refusal convention (not an [error]); the load-bearing
    # proof is that the escaping side effect never happened.

    # --- `..` escape rejected (read + write + edit) ---
    for tool, args in [
        ("read_file", a(path="../../etc/passwd")),
        ("write_file", a(path="../escape.txt", content="pwn")),
        ("edit_file", a(path="../escape.txt", old_string="a", new_string="b")),
    ]:
        out, _ = await call(tool, args)
        assert "[blocked]" in out and "escapes" in out, (tool, out)
    assert not (workdir.parent / "escape.txt").exists(), "write escaped via .."
    print("jail: `..` escapes rejected  ✓")

    # --- absolute path outside workdir rejected ---
    out, _ = await call("read_file", a(path=str(outside / "secret.txt")))
    assert "[blocked]" in out and "TOP SECRET" not in out, out
    out, _ = await call("write_file", a(path=str(outside / "planted.txt"), content="pwn"))
    assert "[blocked]" in out, out
    assert not (outside / "planted.txt").exists(), "absolute write escaped"
    print("jail: absolute paths outside workdir rejected  ✓")

    # --- symlink inside workdir pointing OUT is followed then rejected ---
    link = workdir / "link"
    os.symlink(outside, link)
    out, _ = await call("read_file", a(path="link/secret.txt"))
    assert "[blocked]" in out and "TOP SECRET" not in out, out
    out, _ = await call("write_file", a(path="link/planted.txt", content="pwn"))
    assert "[blocked]" in out, out
    assert not (outside / "planted.txt").exists(), "symlink write escaped"
    print("jail: symlink escape (workdir/link -> outside) rejected  ✓")

    # --- secret files: write/edit refuse them symmetrically with read ---
    (workdir / ".env").write_text("OPENAI_API_KEY=sk-realvalue\n")
    out, _ = await call("read_file", a(path=".env"))
    assert "[blocked]" in out, out
    out, _ = await call("write_file", a(path=".env", content="OPENAI_API_KEY=stolen"))
    assert "[blocked]" in out, out
    assert (workdir / ".env").read_text() == "OPENAI_API_KEY=sk-realvalue\n", "secret .env overwritten!"
    out, _ = await call("write_file", a(path="id_rsa", content="key"))
    assert "[blocked]" in out, out
    assert not (workdir / "id_rsa").exists(), "id_rsa written"
    print("jail: write/edit refuse secret files (.env, id_rsa)  ✓")

    # --- --allow-dir: a HUMAN-declared extra root is in-bounds; nothing else is ---
    extra = Path(tempfile.mkdtemp(prefix="rockyallow-")).resolve()
    (extra / "shared.txt").write_text("shared config\n")
    far = Path(tempfile.mkdtemp(prefix="rockyfar-")).resolve()
    reg2 = tools_mod.build_registry(workdir, allowed_roots=(extra,))

    async def call2(tool, args):
        return await tools_mod.execute(reg2, tool, args)

    # reads/writes inside the declared root now succeed...
    out, ok = await call2("read_file", a(path=str(extra / "shared.txt")))
    assert ok and "shared config" in out, out
    out, ok = await call2("write_file", a(path=str(extra / "note.txt"), content="y"))
    assert ok and "[ok]" in out and (extra / "note.txt").read_text() == "y", out
    # ...and the workdir still works with an extra root present...
    out, ok = await call2("read_file", a(path="ok.txt"))
    assert ok, out
    # ...but a path outside BOTH workdir and the declared root is still blocked.
    out, _ = await call2("write_file", a(path=str(far / "planted.txt"), content="pwn"))
    assert "[blocked]" in out and not (far / "planted.txt").exists(), "escaped the allow-list"
    # Secret refusal is independent of the jail: a secret inside a declared root
    # is STILL refused — widening the jail never opens credential files.
    (extra / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    out, _ = await call2("write_file", a(path=str(extra / ".env"), content="stolen"))
    assert "[blocked]" in out and (extra / ".env").read_text() == "OPENAI_API_KEY=sk-x\n", "secret overwritten!"
    print("jail: --allow-dir opens declared roots only; secrets still refused  ✓")

    print("TOOLS JAIL SMOKE OK — amaze amaze amaze!")


asyncio.run(main())
