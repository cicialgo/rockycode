"""A truncated judge response still drains its trailing usage and is never scored."""
import asyncio
import tempfile
import types
from pathlib import Path

from bench.prompt_budget import Meter
from bench.prompt_review import judge_request


class Stream:
    closed = False
    async def __aiter__(self):
        yield types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(
            delta=types.SimpleNamespace(content='partial'),finish_reason='length')])
        yield types.SimpleNamespace(choices=[],usage=types.SimpleNamespace(
            model_dump=lambda:{'prompt_tokens':100,'completion_tokens':4096}))
    async def close(self):
        self.closed=True


async def main():
    with tempfile.TemporaryDirectory() as td:
        stream=Stream()
        async def create(**kwargs):
            return stream
        client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        meter=Meter(client,Path(td),token_cap=30_000,call_cap=1,turn_cap=1)
        record={'id':'sample-test','raw':'','usage':None,'finish_reason':None}
        await judge_request(client,meter,{'model':'fake','messages':[],'max_tokens':4096},record)
        assert record['finish_reason']=='length' and record['raw']=='partial'
        assert meter.tokens==4196 and meter.calls==1 and not meter.unknown_usage and stream.closed
        assert record['usage']['completion_tokens']==4096
        # The task meter also fails a truncated turn only AFTER its usage arrives.
        stream=Stream()
        other=Path(td)/'task';other.mkdir()
        task_meter=Meter(client,other,token_cap=30_000,call_cap=1,turn_cap=1)
        response=await task_meter.create(model='fake',messages=[],max_tokens=4096)
        receipts=[]
        try:
            async for chunk in response:
                if chunk.usage is not None:
                    receipts.append(chunk.usage)
        except RuntimeError as exc:
            assert 'max_tokens' in str(exc)
        else:
            raise AssertionError('truncated task was accepted')
        assert task_meter.tokens==4196 and not task_meter.unknown_usage and stream.closed
        assert len(receipts)==1, "the engine must receive the final usage before the truncation error"
    print('REVIEW USAGE SMOKE OK — truncated output retained; trailing usage accounted; no retry')


asyncio.run(main())
