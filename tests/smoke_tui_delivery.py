"""Real TUI + Engine with gated fake requests; no API or tool side effects."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["ROCKYCODE_HOME"] = tempfile.mkdtemp(prefix="rocky-delivery-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from invariants import assert_history_api_valid
from rockycode.engine.loop import Engine
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.chatinput import ChatInput


class Script:
    def __init__(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.cleanup = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.order = []
        self.active = self.maximum = 0

    async def create(self, **kwargs):
        prompt = next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")
        self.order.append(prompt)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await self.started.put(prompt)
        try:
            if prompt == "first":
                await self.release.wait()
        except asyncio.CancelledError:
            self.cleanup.set()
            await self.cleanup_release.wait()
            raise
        finally:
            self.active -= 1

        async def stream():
            yield NS(usage=None, choices=[NS(delta=NS(content="done", reasoning_content=None, tool_calls=None))])
        return stream()


async def until(pilot, condition):
    for _ in range(160):
        if condition():
            return
        await pilot.pause(.025)
    raise AssertionError("TUI did not reach expected state")


async def submit(pilot, app, text):
    inp = app.query_one(ChatInput)
    inp.text = text
    inp.focus()
    await pilot.press("enter")


def build(root, language="en"):
    script = Script()
    client = NS(chat=NS(completions=script))
    engine = Engine(model="fake", client=client, registry={}, workdir=root, thinking=False)
    app = RockyCodeApp(engine, evolve="off", ui_language=language)
    engine.hooks.clear()
    app._titled = True  # Title generation is a separate model request, outside delivery.
    return app, engine, script


async def queued_case(root):
    app, engine, script = build(root, "zh")
    async with app.run_test(size=(100, 36)) as pilot:
        await submit(pilot, app, "first")
        await until(pilot, lambda: script.order == ["first"])
        await submit(pilot, app, "/queue on")
        await until(pilot, lambda: app._delivery_mode == "queue")
        await submit(pilot, app, "second")
        await submit(pilot, app, "third")
        await until(pilot, lambda: len(app._delivery.pending) == 2)
        assert script.order == ["first"]
        task = app._delivery.task
        script.release.set()
        await until(pilot, lambda: not app._delivery.running)
        if task:
            await task
        assert script.order == ["first", "second", "third"], script.order
        assert script.maximum == 1
        assert_history_api_valid(engine.history)


async def interrupt_case(root):
    app, engine, script = build(root)
    async with app.run_test(size=(100, 36)) as pilot:
        await submit(pilot, app, "first")
        await until(pilot, lambda: script.order == ["first"])
        script.cleanup_release.clear()
        await submit(pilot, app, "/queue off")  # the default, stated
        await until(pilot, lambda: app._delivery_mode == "interrupt")
        await submit(pilot, app, "urgent")
        await until(pilot, script.cleanup.is_set)
        assert script.order == ["first"]
        script.cleanup_release.set()
        await until(pilot, lambda: script.order == ["first", "urgent"] and not app._delivery.running)
        assert script.maximum == 1
        assert_history_api_valid(engine.history)


async def stop_case(root):
    app, engine, script = build(root)
    async with app.run_test(size=(100, 36)) as pilot:
        await submit(pilot, app, "first")
        await until(pilot, lambda: script.order == ["first"])
        await submit(pilot, app, "/queue on")
        await submit(pilot, app, "later")
        await until(pilot, lambda: len(app._delivery.pending) == 1)
        await pilot.press("escape")
        await until(pilot, lambda: not app._delivery.running)
        assert app._delivery.paused and script.order == ["first"]
        await submit(pilot, app, "/queue resume")
        await until(pilot, lambda: script.order == ["first", "later"] and not app._delivery.running)
        assert_history_api_valid(engine.history)


async def main():
    with tempfile.TemporaryDirectory(prefix="rocky-delivery-") as directory:
        root = Path(directory)
        await queued_case(root)
        await interrupt_case(root)
        await stop_case(root)
    print("TUI DELIVERY OK: FIFO, bilingual UI, cleanup-before-interrupt, stop/resume, valid history")


if __name__ == "__main__":
    asyncio.run(main())
