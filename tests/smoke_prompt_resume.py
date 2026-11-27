"""End-to-end clean budget pause after a write, then resume without replay/billing."""
import asyncio
import copy
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from bench.prompt_ablation import CASES, FIXTURES, capture_workspace, episode, restore_workspace, summarize
from bench.prompt_budget import Meter
from rockycode.engine.providers import resolve
from rockycode.engine.tools import Tool


class Stream:
    def __init__(self, delta):
        self.delta = delta

    async def __aiter__(self):
        yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
            delta=self.delta, finish_reason="tool_calls" if self.delta.tool_calls else "stop")])
        yield types.SimpleNamespace(usage=types.SimpleNamespace(model_dump=lambda: {
            'prompt_tokens': 40, 'completion_tokens': 10}), choices=[])

    async def close(self):
        pass


class Client:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=self)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs['messages']))
        acted = any(m['role'] == 'tool' for m in kwargs['messages'])
        tc = types.SimpleNamespace(index=0, id='increment-once',
            function=types.SimpleNamespace(name='increment', arguments='{}'))
        return Stream(types.SimpleNamespace(reasoning_content=None, content='done' if acted else None,
                                            tool_calls=None if acted else [tc]))


class Sandbox:
    def __init__(self, root):
        self.root = root

    @classmethod
    async def start(cls, workdir, **kwargs):
        return cls(workdir)

    async def stop(self):
        pass


def registry(sandbox):
    async def increment():
        path = sandbox.root / 'count.txt'
        path.write_text(str(int(path.read_text()) + 1))
        return 'incremented once'
    return {'increment': Tool('increment', {}, increment)}


async def main(step_cap=0):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        client = Client()
        spec = dict(case='resume_test', language='en', arm='adapted', rep=1)
        args = types.SimpleNamespace(max_output=128, effort='low', turn_timeout=30, engine_max_steps=step_cap)
        dest = root / 'episode'
        dest.mkdir()
        CASES['resume_test'] = dict(en=['increment once then report'], write_turns=[1], acceptance=None)
        FIXTURES['resume_test'] = {'count.txt': '0'}
        resolved = resolve('deepseek-v4-pro')
        with patch('rockycode.engine.sandbox.ChatSandbox', Sandbox), patch('rockycode.engine.sandbox.build_sandbox_registry', registry):
            meter = Meter(client, root, token_cap=100_000, call_cap=1, turn_cap=10)
            row = await episode(spec, 'system', args, meter, 'fake-image', dest, resolved)
            assert row['status'] == 'budget_exhausted' and not row['mechanical_pass']
            assert len(client.requests) == 1 and meter.tokens == 50
            saved = json.loads((dest / 'checkpoint.json').read_text())
            assert saved['loop_state']['step'] == 2
            assert saved['loop_state']['max_steps'] == step_cap
            assert saved['history'][-1]['role'] == 'tool'
            assert len(row['partial_turn']['calls']) == 1
            assert summarize([row])['adapted']['completed'] == 0
            assert summarize([row])['adapted']['tokens'] == 50
            assert saved['partial']['elapsed_seconds'] >= 0
            if step_cap:
                expired = copy.deepcopy(saved)
                expired['partial']['elapsed_seconds'] = args.turn_timeout
                (dest / 'checkpoint.json').write_text(json.dumps(expired))
                limited = Meter(client, root, token_cap=100_000, call_cap=3, turn_cap=10, restore=True)
                timed_out = await episode(spec, 'system', args, limited, 'fake-image', dest, resolved)
                assert timed_out['status'] == 'error' and len(client.requests) == 1
                assert 'TimeoutError' in str(timed_out['error'])
                # Return to the original checkpoint to exercise the independent
                # successful continuation path; no paid call occurred above.
                (dest / 'checkpoint.json').write_text(json.dumps(saved))
            resumed = Meter(client, root, token_cap=100_000, call_cap=3, turn_cap=10, restore=True)
            row = await episode(spec, 'system', args, resumed, 'fake-image', dest, resolved)
            assert row['status'] == 'completed' and row['mechanical_pass']
            assert len(client.requests) == 2 and resumed.calls == 2 and resumed.tokens == 100
            assert len([m for m in client.requests[-1] if m['role'] == 'user']) == 1
            assert row['turns'][0]['usage']['prompt_tokens'] == 80
            assert json.loads((dest / 'files-turn-1.json').read_text())['count.txt'] == '1'
            assert len(row['turns'][0]['calls']) == 1
            # Resuming an already completed checkpoint does not call the API again.
            await episode(spec, 'system', args, resumed, 'fake-image', dest, resolved)
            assert len(client.requests) == 2
            try:
                await episode(spec, 'CHANGED', args, resumed, 'fake-image', dest, resolved)
            except ValueError as exc:
                assert 'mismatch' in str(exc)
            else:
                raise AssertionError('changed prompt resumed')
        ledger = json.loads((root / 'ledger.json').read_text())
        ledger['unknown_usage'] = True
        (root / 'ledger.json').write_text(json.dumps(ledger))
        try:
            Meter(client, root, token_cap=100_000, call_cap=3, turn_cap=10, restore=True)
        except RuntimeError as exc:
            assert 'unreconciled' in str(exc)
        else:
            raise AssertionError('unknown spending replayed')
        source, target = root / 'binary', root / 'restored'
        source.mkdir(); target.mkdir()
        (source / 'file.bin').write_bytes(b'\x00\xff\x01')
        restore_workspace(target, capture_workspace(source))
        assert (target / 'file.bin').read_bytes() == b'\x00\xff\x01'
        try:
            restore_workspace(target, {'../escape': {'data': '', 'mode': 0o644}})
        except ValueError:
            pass
        else:
            raise AssertionError('unsafe checkpoint path accepted')
    print('PROMPT RESUME SMOKE OK — one write, two paid calls, cumulative usage, no duplicate user turn')


asyncio.run(main())
asyncio.run(main(3))
