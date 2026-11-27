"""Waiting for a widget to be gone, without ending the app.

`await widget.remove()` joins Textual's own prune task, and the App's message
loop waits for that same task. So when the coroutine that awaits a removal is
cancelled (Esc, a new message, leaving), the cancel reaches the prune task and
the App's loop ends with it: rocky closed by itself, without a word.

Behind a shield, our cancel stops only our wait. The removal goes on.
"""
from __future__ import annotations

import asyncio


async def gone(widget) -> None:
    """Remove *widget* and wait until it is gone. Safe to be cancelled in."""
    await asyncio.shield(widget.remove())
