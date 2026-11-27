"""Neutral finite budgets, unlimited chat, and clean-boundary continuation."""
import asyncio
import copy
import tempfile
import types
from pathlib import Path

from rockycode.engine.events import EngineError, TurnFinished
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool


class Model:
    def __init__(self, answer_at=999):
        self.requests = []
        self.answer_at = answer_at

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs['messages']))
        n = len(self.requests)
        tc = types.SimpleNamespace(index=0, id=f'c{n}', function=types.SimpleNamespace(name='observe', arguments='{}'))
        delta = types.SimpleNamespace(reasoning_content=None, content='done' if n == self.answer_at else None,
                                      tool_calls=None if n == self.answer_at else [tc])
        async def stream():
            yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
                delta=delta, finish_reason="tool_calls" if delta.tool_calls else "stop")])
        return stream()


async def observe():
    return 'evidence'


async def run(root, cap=None, finalize=None, answer_at=999):
    model = Model(answer_at)
    kw = {} if cap is None else {'max_steps': cap}
    if finalize is not None:
        kw['finalize_steps'] = finalize
    engine = Engine(model='fake', client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=model)),
                    workdir=root, registry={'observe': Tool('observe', {}, observe, risk='safe')}, **kw)
    events = [e async for e in engine.run_turn('investigate')]
    return engine, model, events


def notices(messages):
    return [m['content'] for m in messages if m['role'] == 'user' and str(m.get('content', '')).startswith('[harness]')]


async def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        engine, model, events = await run(root)
        assert len(model.requests) == 110
        assert not notices(model.requests[49]), 'step 50 must not pressure the default run'
        assert len(notices(model.requests[79])) == 1
        assert '31 model calls remain' in notices(model.requests[79])[0]
        assert len(notices(model.requests[100])) == 2
        assert '10 model calls remain' in notices(model.requests[100])[-1]
        assert any(isinstance(e, EngineError) and '100+10 finalize' in e.message for e in events)
        assert any(isinstance(e, TurnFinished) for e in events)
        # Explicit finite limits and zero closing window still work.
        _, small, _ = await run(root, 13, 3)
        assert len(small.requests) == 16
        assert '6 model calls remain' in notices(small.requests[10])[0]
        _, tiny, _ = await run(root, 2, 0)
        assert len(tiny.requests) == 2
        _, unlimited, _ = await run(root, 0, answer_at=120)
        assert len(unlimited.requests) == 120 and not notices(unlimited.requests[-1])
        # A completed answer may finish early; a budget never forces extra work.
        _, early, _ = await run(root, answer_at=2)
        assert len(early.requests) == 2
        try:
            async for _ in engine.resume_turn():
                pass
        except ValueError:
            pass
        else:
            raise AssertionError('resume reset a finite cap')
        engine.max_steps = 0
        engine.history = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'},
                          {'role': 'assistant', 'tool_calls': [{'id': 'lost', 'type': 'function',
                            'function': {'name': 'observe', 'arguments': '{}'}}]}]
        original = copy.deepcopy(engine.history)
        try:
            async for _ in engine.resume_turn():
                pass
        except ValueError:
            pass
        else:
            raise AssertionError('unresolved tools must not be replayed')
        assert engine.history == original
    print('BUDGET SMOKE OK — neutral notices; 100+10 default; unlimited chat; no cap-reset resume')


asyncio.run(main())
