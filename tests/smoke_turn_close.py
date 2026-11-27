"""Closing the outer event generator must synchronously repair pending tool history."""
import asyncio
import tempfile
import types
from pathlib import Path

from rockycode.engine.loop import Engine
from rockycode.engine.events import ToolStarted
from rockycode.engine.tools import Tool


async def main():
    calls = []
    async def execute():
        calls.append('executed')
        return 'ok'
    async def create(**kwargs):
        tc=types.SimpleNamespace(index=0,id='pending',function=types.SimpleNamespace(name='action',arguments='{}'))
        delta=types.SimpleNamespace(reasoning_content=None,content=None,tool_calls=[tc])
        async def stream():
            yield types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(delta=delta,finish_reason='tool_calls')])
        return stream()
    with tempfile.TemporaryDirectory() as td:
        for resume in (False, True):
            engine=Engine(model='fake',client=types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create))),workdir=Path(td),max_steps=0,
                registry={'action':Tool('action',{},execute)})
            if resume:
                engine.history.append({'role':'user','content':'continue original request'})
            stream=engine.resume_turn() if resume else engine.run_turn('inspect')
            async for event in stream:
                if isinstance(event,ToolStarted):
                    break
            await stream.aclose()
            # No scheduling tick / GC may be needed to make the history valid.
            assert any(m['role']=='tool' and m['tool_call_id']=='pending' for m in engine.history), engine.history
            assert not calls, 'closing before execution must not run the tool'
    print('TURN CLOSE SMOKE OK — outer close repairs inner history before returning')


asyncio.run(main())
