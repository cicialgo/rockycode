"""Finite continuation preserves paid steps, notices, tool effects and full usage."""
import asyncio
import copy
import tempfile
import types
from pathlib import Path

from bench.prompt_budget import Meter, BudgetStop
from rockycode.engine.loop import Engine
from rockycode.engine.tools import Tool
from rockycode.engine.events import EngineError,TurnFinished


class Model:
    def __init__(self):
        self.requests=[]

    async def create(self,**kwargs):
        self.requests.append(copy.deepcopy(kwargs['messages']))
        n=len(self.requests)
        delta=types.SimpleNamespace(reasoning_content=None,content=None,
            tool_calls=[types.SimpleNamespace(index=0,id=f'c{n}',
                function=types.SimpleNamespace(name='record',arguments='{}'))])
        class Stream:
            def __aiter__(self):
                return self.items()
            async def items(self):
                yield types.SimpleNamespace(usage=types.SimpleNamespace(model_dump=lambda:{'prompt_tokens':10,'completion_tokens':5}),
                    choices=[types.SimpleNamespace(delta=delta,finish_reason='tool_calls')])
            async def close(self):
                pass
        return Stream()


async def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        model=Model();client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=model))
        effects=[]
        async def record():
            effects.append(len(effects)+1)
            return 'done'
        def make_engine(meter):
            return Engine(model='fake',client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=meter)),
                workdir=root,registry={'record':Tool('record',{},record,risk='safe')},
                max_steps=4,finalize_steps=2,max_tokens=1024,context_window=100000)
        saved=None
        for cap in [3,4,20]:
            meter=Meter(client,root,token_cap=100000,call_cap=100,turn_cap=cap,episode_cap=100000,restore=saved is not None)
            engine=make_engine(meter)
            if saved:
                engine.history=copy.deepcopy(saved['history'])
            checkpoints=[]
            def checkpoint():
                state=engine.export_turn_state()
                assert state is not None
                checkpoints.append({'history':copy.deepcopy(engine.history),'state':state})
            meter.before_request=checkpoint
            stream=engine.resume_turn(state=saved['state']) if saved else engine.run_turn('investigate')
            events=[e async for e in stream]
            if cap<20:
                assert any(isinstance(e,EngineError) and 'BudgetStop' in e.message for e in events)
                saved=checkpoints[-1]
                assert saved['state']['step']==cap+1
                assert saved['state']['used_tools']
                assert saved['state']['phase']=='request'
                assert meter.calls==cap and len(effects)==cap
                # Unknown/mismatched state must not create a paid call.
                before=len(model.requests)
                altered=copy.deepcopy(saved['state']);altered['max_steps']=100
                try:
                    async for _ in engine.resume_turn(state=altered):
                        pass
                except ValueError:
                    pass
                else:
                    raise AssertionError('changed cap accepted')
                altered=copy.deepcopy(saved['state']);altered['history_sha256']='wrong'
                try:
                    async for _ in engine.resume_turn(state=altered):
                        pass
                except ValueError:
                    pass
                else:
                    raise AssertionError('changed history accepted')
                assert len(model.requests)==before
            else:
                assert any(isinstance(e,EngineError) and 'step limit (4+2' in e.message for e in events)
                finished=[e for e in events if isinstance(e,TurnFinished)]
                assert finished[-1].usage['prompt_tokens']==60
                assert engine.export_turn_state() is None
                assert meter.calls==6 and meter.tokens==90 and len(effects)==6
        def notices(ms):
            return [m for m in ms if str(m.get('content','')).startswith('[harness]')]
        assert len(model.requests)==6
        assert len(notices(model.requests[3]))==1  # pause at warning boundary, no duplicate
        assert len(notices(model.requests[4]))==2  # pause at closing boundary, no duplicate
        assert len(notices(model.requests[5]))==2
        assert sum(m['role']=='user' and m['content']=='investigate' for m in engine.history)==1
    print('FINITE RESUME SMOKE OK — six calls total; no repeated tools/notices; cumulative usage; state binding')


async def compaction_boundaries():
    """A paid compaction request must not replay when the main request pauses."""
    class CompactEngine(Engine):
        async def _maybe_compact(self, total):
            if not any(m.get('content')=='compacted' for m in self.history):
                stream=await self.client.chat.completions.create(model=self.model,
                    messages=self.history, max_tokens=64, stream=True)
                async for chunk in stream:
                    if chunk.usage:
                        for key,value in chunk.usage.model_dump().items():
                            total[key]=total.get(key,0)+value
                self._append({'role':'user','content':'compacted'})
            if False:
                yield
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);model=Model()
        client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=model))
        effects=[]
        async def record():
            effects.append(1)
            return 'done'
        saved=None
        for cap in [0,1,20]:
            meter=Meter(client,root,token_cap=100000,episode_cap=100000,call_cap=100,turn_cap=cap,restore=saved is not None)
            e=CompactEngine(model='fake',client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=meter)),
                workdir=root,registry={'record':Tool('record',{},record,risk='safe')},max_steps=2,finalize_steps=1,max_tokens=64)
            if saved:
                e.history=copy.deepcopy(saved['history'])
            points=[]
            def save():
                points.append({'history':copy.deepcopy(e.history),'state':e.export_turn_state()})
            meter.before_request=save
            try:
                stream=e.resume_turn(state=saved['state']) if saved else e.run_turn('investigate')
                events=[event async for event in stream]
            except RuntimeError as exc:
                assert cap==0 and isinstance(exc,BudgetStop)
            if cap<20:
                saved=points[-1]
                assert saved['state']['step']==1
                assert saved['state']['phase']==('compact' if cap==0 else 'request')
                assert len(model.requests)==cap
            else:
                assert len(model.requests)==4 and len(effects)==3
                done=[event for event in events if isinstance(event,TurnFinished)][-1]
                assert done.usage['prompt_tokens']==40 and done.usage['completion_tokens']==20
                assert sum(m.get('content')=='compacted' for m in e.history)==1
    print('COMPACTION RESUME OK — before/after summary boundaries, no repeated paid summary')


asyncio.run(main())
asyncio.run(compaction_boundaries())
