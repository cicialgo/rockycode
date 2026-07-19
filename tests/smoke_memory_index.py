"""Memory index smoke test: fake embedding client, real sqlite-vec.

No Ollama needed — the fake embedder maps texts onto deterministic unit
vectors so nearest-neighbor results are predictable. Covers language
routing, incremental reindex, hybrid search, Ollama-down degradation.
"""
import asyncio
import os
import tempfile
import types
from pathlib import Path

os.chdir(tempfile.mkdtemp(prefix="rockysmoke-"))

from rockycode.memory import Memory, MemoryStore
from rockycode.memory.index import EMBED_MODELS, MemoryIndex, detect_lang

# language routing
assert detect_lang("run the tests with uv") == "en"
assert detect_lang("用 uv 跑测试，记住这个") == "zh"
assert detect_lang("fix bug in 模块 loader 里面") == "zh"  # mixed → zh model handles both
print("lang routing OK")


class FakeEmbeddings:
    """Deterministic: every text maps to a unit vector on an axis chosen by
    which keyword it contains, so cosine neighbors are knowable in advance."""

    AXES = ["docker", "tests", "颜色"]

    def __init__(self):
        self.calls = 0
        self.down = False

    async def create(self, *, model, input):
        if self.down:
            raise ConnectionError("ollama is down")
        self.calls += 1
        dim = next(d for m, d, _, _ in EMBED_MODELS.values() if m == model)
        out = []
        for text in input:
            vec = [0.0] * dim
            axis = next((i for i, kw in enumerate(self.AXES) if kw in text), len(self.AXES))
            vec[axis] = 1.0
            out.append(types.SimpleNamespace(embedding=vec))
        return types.SimpleNamespace(data=out)


fake = FakeEmbeddings()
client = types.SimpleNamespace(embeddings=fake)

store = MemoryStore.for_workdir(Path.cwd())
store.save(Memory(name="docker-rosetta", type="fact", description="docker needs rosetta on apple silicon",
                  body="Enable rosetta for x86 docker images on apple silicon."))
store.save(Memory(name="tests-are-smoke", type="fact", description="tests are smoke scripts",
                  body="tests/ holds smoke scripts, no pytest."))
store.save(Memory(name="ui-hex-colors", type="feedback", description="界面颜色规则",
                  body="界面颜色只用十六进制，不用 ANSI 颜色名。"))

index = MemoryIndex(store, client=client)


async def main():
    indexed, kept, removed = await index.reindex()
    assert (indexed, kept, removed) == (3, 0, 0), (indexed, kept, removed)
    # every doc lands in BOTH spaces — cross-lingual recall depends on it
    assert index.conn().execute("SELECT count(*) FROM vec_zh").fetchone()[0] == 3
    assert index.conn().execute("SELECT count(*) FROM vec_en").fetchone()[0] == 3

    # incremental: nothing changed → nothing re-embedded
    indexed, kept, removed = await index.reindex()
    assert (indexed, kept) == (0, 3), (indexed, kept)

    # semantic hit: query about docker lands on the docker memory first
    hits = await index.search("docker emulation help", k=2)
    assert hits and hits[0][0].name == "docker-rosetta", [m.name for m, _ in hits]

    # chinese query → zh table → the chinese memory
    hits = await index.search("颜色 应该 用 什么", k=2)
    assert hits and hits[0][0].name == "ui-hex-colors", [m.name for m, _ in hits]

    # archive removes from the index on next pass
    store.archive("tests-are-smoke")
    _, _, removed = await index.reindex()
    assert removed == 1
    print("index + search OK")

    # ollama down → FTS5 keyword still answers, nothing raises
    fake.down = True
    hits = await index.search("rosetta", k=2)
    assert hits and hits[0][0].name == "docker-rosetta", [m.name for m, _ in hits]
    print("degradation OK")


asyncio.run(main())
print("SMOKE OK — rocky find memory by meaning. amaze!")
