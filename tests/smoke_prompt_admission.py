"""A matched block cannot be half-started; unsafe resume preserves all evidence."""
import asyncio
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from bench.prompt_ablation import live


class Client:
    instances = []

    def __init__(self, **kwargs):
        self.closed = False
        self.chat = types.SimpleNamespace(completions=self)
        Client.instances.append(self)

    async def create(self, **kwargs):
        raise AssertionError('an unadmitted block must not make a paid request')

    async def close(self):
        self.closed = True


def shell(argv, **kwargs):
    if argv[0] == 'docker':
        return 'sha256:fixture\n'
    if argv[1] == 'rev-parse':
        return 'fixture-commit\n'
    return ''


async def main():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        args = types.SimpleNamespace(model='fake', image='fake', resume=None, token_cap=10_000,
            call_cap=100, turn_call_cap=10, episode_token_cap=100_000, order_seed=1)
        plan = [dict(case='explain', language='en', arm=a, rep=1) for a in ('adapted', 'grounded')]
        resolved = (types.SimpleNamespace(name='fake'), types.SimpleNamespace(key=lambda: 'fake-key',
                                                                             base_url='https://unused.test'), 'fake')
        with patch('openai.AsyncOpenAI', Client), patch('rockycode.onboarding.bootstrap_credentials', lambda: None), \
             patch('rockycode.engine.providers.resolve', lambda _: resolved), patch('subprocess.check_output', shell):
            await live(args, plan, out)
            summary = json.loads((out/'summary.json').read_text())
            assert summary['completed'] == summary['attempted'] == summary['api_calls'] == 0
            assert summary['not_started'] == 2
            assert 'paired block' in summary['stop_reason']
            assert not list(out.glob('*/checkpoint.json'))
            # A zero-call pause can itself be resumed without a checkpoint.
            args.resume = str(out)
            await live(args, plan, out)
            assert json.loads((out/'summary.json').read_text())['api_calls'] == 0
            ledger = json.loads((out/'ledger.json').read_text())
            ledger['unknown_usage'] = True
            (out/'ledger.json').write_text(json.dumps(ledger))
            before = {p.name: p.read_bytes() for p in out.glob('*.json')}
            try:
                await live(args, plan, out)
            except RuntimeError as exc:
                assert 'unreconciled' in str(exc)
            else:
                raise AssertionError('unsafe restore was accepted')
            assert before == {p.name: p.read_bytes() for p in out.glob('*.json')}, 'rejected resume changed saved evidence'
            assert all(c.closed for c in Client.instances)
    print('PROMPT ADMISSION SMOKE OK — whole-block reserve; zero-call resume; refused resume preserves artifacts')


asyncio.run(main())
