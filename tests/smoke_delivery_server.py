"""Exercise actual serve dispatch with gated fake model requests."""
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rocky-delivery-serve-home-")
from rockycode.engine.loop import Engine
from rockycode import server


async def main():
    records = []
    started = asyncio.Queue()
    release = asyncio.Event()
    order = []
    managers = []
    real_manager = server.SessionManager

    async def create(**kwargs):
        prompt = next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")
        order.append(prompt)
        await started.put(prompt)
        if prompt in ("first", "hold"):
            await release.wait()

        async def stream():
            yield NS(usage=None, choices=[NS(delta=NS(content="done", reasoning_content=None, tool_calls=None))])
        return stream()

    client = NS(chat=NS(completions=NS(create=create)))

    def manager(**kwargs):
        instance = real_manager(**kwargs)
        managers.append(instance)
        return instance

    def response(id_):
        return next(record for record in reversed(records) if record.get("id") == id_)

    async def messages():
        yield {"method": "initialize", "id": 1, "params": {}}
        sid = response(1)["result"]["session_id"]
        managers[0].get(sid).engine.hooks.clear()  # Delivery, not self-evaluation.
        args = {"session_id": sid}
        yield {"method": "chat", "id": 2, "params": {**args, "message": "first"}}
        assert response(2)["result"]["status"] == "started"
        assert await started.get() == "first"
        yield {"method": "chat", "id": 3, "params": {**args, "message": "second", "delivery": "queue"}}
        yield {"method": "chat", "id": 4, "params": {**args, "message": "third", "delivery": "queue"}}
        assert response(3)["result"]["status"] == "queued"
        yield {"method": "get_status", "id": 5, "params": args}
        assert len(response(5)["result"]["delivery"]["pending"]) == 2
        yield {"method": "chat", "id": 6, "params": {**args, "message": "bad", "delivery": "invalid"}}
        assert response(6)["error"]["code"] == -32602 and order == ["first"]
        yield {"method": "queue/status", "id": 7, "params": {"session_id": "missing"}}
        assert response(7)["error"]["code"] == -32602
        task = managers[0].get(sid).delivery.task
        release.set()
        await task
        assert order == ["first", "second", "third"]
        release.clear()
        yield {"method": "chat", "id": 8, "params": {**args, "message": "hold"}}
        while await started.get() != "hold":
            pass
        yield {"method": "chat", "id": 9, "params": {**args, "message": "later", "delivery": "queue"}}
        task = managers[0].get(sid).delivery.task
        yield {"method": "cancel", "id": 10, "params": args}
        await task
        yield {"method": "queue/status", "id": 11, "params": args}
        assert response(11)["result"]["paused"] and len(response(11)["result"]["pending"]) == 1
        yield {"method": "queue/resume", "id": 12, "params": args}
        await managers[0].get(sid).delivery.task
        assert order[-1] == "later"
        yield {"method": "shutdown", "id": 13, "params": {}}

    with tempfile.TemporaryDirectory(prefix="rocky-delivery-serve-") as workdir:
        with patch.object(server, "Engine", lambda **kwargs: Engine(client=client, **kwargs)), \
                patch.object(server, "SessionManager", manager), \
                patch.object(server, "_read_messages", messages), \
                patch.object(server, "_write_line", records.append):
            await server.run_server(model="fake", workdir=Path(workdir))
    assert response(13)["result"]["ok"]
    print("SERVE DELIVERY OK: dispatch, legacy interrupt, FIFO, validation, stop/status/resume")


if __name__ == "__main__":
    asyncio.run(main())
