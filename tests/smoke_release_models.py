"""Offline SDK/Engine protocol gate, also run with -I against an installed wheel."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile

# Always isolate user overrides; the release gate evaluates the bundled catalog.
with tempfile.TemporaryDirectory(prefix='rocky-release-models-') as home:
    os.environ['ROCKYCODE_HOME'] = home
    os.environ['ROCKYCODE_OLLAMA_URL'] = 'http://127.0.0.1:9'

    import httpx
    from openai import AsyncOpenAI
    import rockycode
    from rockycode.engine import providers
    from rockycode.engine.loop import Engine
    import rockycode.runtime  # desktop/source clients require this core module

    if os.environ.get('RELEASE_INSTALLED_ROOT'):
        installed_root = Path(os.environ['RELEASE_INSTALLED_ROOT']).resolve()
        assert Path(rockycode.__file__).resolve().is_relative_to(installed_root), rockycode.__file__
        assert version('rockycode') == os.environ['RELEASE_EXPECTED_VERSION']
    assert providers.BUILTIN_MODELS_TOML.is_file()

    async def request(model: str, dial: str) -> dict:
        captured = []

        async def reply(req: httpx.Request) -> httpx.Response:
            assert str(req.url) == 'https://release.invalid/v1/chat/completions'
            captured.append(json.loads(req.content))
            chunk = {'id': 'release-fixture', 'object': 'chat.completion.chunk',
                     'created': 0, 'model': captured[-1]['model'],
                     'choices': [{'index': 0, 'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')

        transport = httpx.MockTransport(reply)
        async with AsyncOpenAI(api_key='offline-test', base_url='https://release.invalid/v1',
                               max_retries=0,
                               http_client=httpx.AsyncClient(transport=transport, trust_env=False)) as client:
            provider, endpoint, resolved = providers.resolve(model)
            engine = Engine(model=resolved, client=client, workdir=Path(home), registry={},
                            thinking=dial != 'off', reasoning_effort='max' if dial == 'off' else dial,
                            max_tokens=2048, system_prompt='Offline release fixture.')
            engine.adopt(provider, endpoint, resolved)
            async for _event in engine.run_turn('Reply ok.'):
                pass
            assert engine.stats.engine_errors == 0
        assert len(captured) == 1, captured
        body = captured[0]
        assert body['max_tokens'] == 2048 and body['stream'] is True
        assert body['stream_options'] == {'include_usage': True}
        return body

    async def check():
        count = 0
        for dial in ('off', 'low', 'high', 'max'):
            baseline = await request('deepseek-flash', dial)
            for alias in ('deepseek-v4-flash', 'deepseek:deepseek-v4-flash'):
                assert await request(alias, dial) == baseline, (dial, alias)
                count += 1
            assert baseline['model'] == 'deepseek-flash'
            assert baseline['thinking'] == {'type': 'disabled' if dial == 'off' else 'enabled'}
            if dial == 'off':
                assert 'reasoning_effort' not in baseline
            else:
                assert baseline['reasoning_effort'] == dial
            minimax = await request('minimax-m3.1-flash', dial)
            assert minimax['model'] == 'MiniMax-M3.1-Flash'
            assert minimax['thinking'] == {'type': 'adaptive'}
            assert minimax['reasoning_split'] is True
            assert minimax['reasoning_effort'] == ('low' if dial == 'off' else dial)
            count += 2
        # The new per-model policy must not change the old M3's ability to turn off.
        old = await request('minimax-m3', 'off')
        assert old['model'] == 'minimax-m3' and old['thinking'] == {'type': 'disabled'}
        assert 'reasoning_effort' not in old
        print(f'RELEASE MODELS OK — {count + 1} offline SDK requests; bundled catalog + core protocol')

    asyncio.run(check())
