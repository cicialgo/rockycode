"""Host-owned FIFO delivery; no second model loop and no automatic replay."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable


class DeliveryError(ValueError):
    """A refused submit. `code` is stable so a host can localize it:
    mode | length | closed | busy | full. The message stays English."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class TurnMessage:
    prompt: str
    images: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str = "queue"

    def snapshot(self) -> dict:
        return {"id": self.id, "prompt": self.prompt, "images": list(self.images), "mode": self.mode}


class TurnDelivery:
    """One active turn. Interrupt waits for cleanup; stop/error pauses the FIFO.

    The host runner returns True only for a completed turn. Pending messages
    may be stored by on_change, but restored queues always require resume.
    """

    def __init__(self, run: Callable[[TurnMessage], Awaitable[bool]], *, on_change=None,
                 pending=(), max_pending=32):
        self.run = run
        self.on_change = on_change or (lambda: None)
        self.pending = [TurnMessage(**item) for item in pending]
        self.paused = bool(self.pending)
        self.max_pending = max_pending
        self.current: TurnMessage | None = None
        self.task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def snapshot(self) -> dict:
        return {"pending": [item.snapshot() for item in self.pending], "paused": self.paused,
                "active": self.current.snapshot() if self.current else None}

    async def submit(self, prompt: str, *, mode="queue", images=()) -> dict:
        if mode not in ("queue", "interrupt", "reject"):
            raise DeliveryError("mode", "delivery must be queue, interrupt or reject")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise DeliveryError("length", "message must contain 1–100000 characters")
        async with self._lock:
            if self._closed:
                raise DeliveryError("closed", "delivery is closed")
            if self.paused and not self.pending and not self.running:
                # A stop (or a restored queue) pauses the FIFO so nothing resumes
                # unattended — but a fresh message with nothing queued is the
                # user acting now. Run it; never park it behind a Resume button.
                self.paused = False
            if mode == "reject" and (self.running or self.pending):
                raise DeliveryError("busy", "a turn is already active or queued")
            if len(self.pending) >= self.max_pending:
                raise DeliveryError("full", "message queue is full")
            item = TurnMessage(prompt, list(images), mode=mode)
            if mode == "interrupt":
                self.paused = True
                await self._cancel_active()
                self.pending.insert(0, item)
                self.paused = False
            else:
                self.pending.append(item)
            started = not self.running and not self.paused
            self.on_change()
            self._start()
            return {"message_id": item.id, "status": "started" if started else "queued"}

    def _start(self):
        if not self._closed and not self.paused and self.pending and not self.running:
            self.task = asyncio.create_task(self._drain())

    async def _drain(self):
        try:
            while self.pending and not self.paused and not self._closed:
                self.current = self.pending.pop(0)
                self.on_change()
                if not await self.run(self.current):
                    self.paused = True
                self.current = None
                self.on_change()
        except asyncio.CancelledError:
            self.paused = True
            raise
        except Exception:
            self.paused = True
            raise
        finally:
            self.current = None
            self.task = None
            self.on_change()

    async def _cancel_active(self):
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop(self):
        async with self._lock:
            active = self.running
            self.paused = True
            await self._cancel_active()
            self.on_change()
            return active

    async def resume(self):
        async with self._lock:
            if self._closed:
                raise DeliveryError("closed", "delivery is closed")
            self.paused = False
            self.on_change()
            self._start()

    async def remove(self, message_id: str):
        async with self._lock:
            before = len(self.pending)
            self.pending = [item for item in self.pending if item.id != message_id]
            self.on_change()
            return len(self.pending) != before

    async def clear(self):
        async with self._lock:
            self.pending.clear()
            self.on_change()

    async def close(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self.paused = True
            await self._cancel_active()
            self.on_change()
