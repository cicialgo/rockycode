"""Offline integration with the real shared core (requires its Python env)."""
import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from rockycode.runtime import build_runtime
except ImportError:
    build_runtime = None
from rockycode_desktop.adapter import EngineAdapter
from rockycode_desktop.host import Host


def fake_client(replies):
    calls = iter(replies)

    async def create(**kw):
        reply = next(calls)
        delta = NS(content=reply if isinstance(reply, str) else None, reasoning_content=None, tool_calls=None)
        if isinstance(reply, tuple):
            name, args = reply
            delta.tool_calls = [NS(index=0, id='call-' + name, function=NS(name=name, arguments=json.dumps(args)))]

        async def stream():
            yield NS(usage=None, choices=[NS(delta=delta,
                finish_reason="tool_calls" if delta.tool_calls else "stop")])
        return stream()

    return NS(chat=NS(completions=NS(create=create)), close=AsyncMock())


def hermetic_env(home, **extra):
    """Exactly one fixture key and a throwaway home — never the developer's own
    key file or keychain. The suite then runs the same on a clean machine."""
    kept = {k: v for k, v in os.environ.items()
            if not re.fullmatch(r'ROCKYCODE_(\w+_)?(API_KEY|BASE_URL)|ROCKYCODE_MODEL', k)}
    return patch.dict(os.environ, {**kept, 'ROCKYCODE_HOME': str(home), 'ROCKYCODE_MODEL': 'fake',
                                   'ROCKYCODE_API_KEY': 'fixture-home', 'ROCKYCODE_OLLAMA_URL': 'http://127.0.0.1:9',
                                   **extra}, clear=True)


@unittest.skipUnless(build_runtime, 'requires shared RockyCode Runtime and Python 3.11+')
class SharedHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rocky-host-core-')
        self.root = Path(self.temp.name)
        self.env = hermetic_env(self.root / 'global')
        self.env.start()
        self.credentials = patch('rockycode.onboarding.bootstrap_credentials')
        self.credentials.start()
        import rockycode.session as session
        self.registry = patch.object(session, 'REGISTRY', self.root / 'global/projects.json')
        self.registry.start()
        self.host = Host(str(self.root / 'data'))
        self.thread = await self.host.dispatch('thread/create', {'workspace': str(self.root)})
        self.args = {'threadId': self.thread['id']}

    async def asyncTearDown(self):
        await self.host.close()
        self.registry.stop()
        self.credentials.stop()
        self.env.stop()
        self.temp.cleanup()

    def factory(self, client):
        return lambda *args: EngineAdapter(*args, client=client, include_user=False, mcp=False, lsp=False)

    async def drain(self, allowed=True, decision=None):
        async with asyncio.timeout(15):
            while self.host.tasks:
                approval = self.thread['approval']
                if approval:
                    respond = decision if decision is not None else ('once' if allowed else 'deny')
                    await self.host.dispatch('approval/respond', {**self.args, 'approvalId': approval['id'], 'decision': respond})
                await asyncio.sleep(.01)
        self.assertEqual(self.thread['messages'][-1]['status'], 'complete', self.thread['messages'][-1].get('error'))

    async def test_real_tools_warm_session_and_shutdown(self):
        (self.root / 'input.txt').write_text('source')
        client = fake_client([('read_file', {'path': 'input.txt'}), ('write_file', {'path': 'output.txt', 'content': 'source'}), 'done', 'again'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'read and write'})
            await self.drain()
            adapter = self.host.adapters[self.thread['id']]
            # Each tool row says what the call acted on — the file, never its content.
            rows = [(tool['name'], tool['detail']) for message in self.thread['messages'] for tool in message.get('tools', [])]
            self.assertEqual(rows, [('read_file', 'input.txt'), ('write_file', 'output.txt · 1 line')])
            self.assertEqual((self.root / 'output.txt').read_text(), 'source')
            self.assertIn('web_search', self.thread['tools'])
            self.assertIn('recall_memory', self.thread['tools'])
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'continue'})
            await self.drain()
            self.assertIs(adapter, self.host.adapters[self.thread['id']])
        await self.host.close()
        client.close.assert_awaited_once()

    async def test_denial_has_no_write(self):
        # bash is ask-tier; an in-workdir write_file now passes silently under
        # the shared ask-mode policy, so denial is exercised through bash.
        client = fake_client([('bash', {'command': 'touch denied.txt'}), 'denied'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'denied test'})
            await self.drain(allowed=False)
        self.assertFalse((self.root / 'denied.txt').exists())
        self.assertIn('[denied]', self.host.store.history(self.thread['id'])[-2]['content'])

    async def test_yolo_mode_suppresses_bash_prompts(self):
        # bash is risky-tier (asks in "ask"); yolo auto-approves it — the drain
        # below never answers an approval, so any prompt would time out the run.
        client = fake_client([('bash', {'command': 'echo hi > yolo.txt'}), 'done'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'yolo test'})
            await self.drain(allowed=False)  # would deny every prompt if one appeared
        self.assertEqual((self.root / 'yolo.txt').read_text().strip(), 'hi')
        self.assertIsNone(self.thread['approval'])

    async def test_yolo_external_reads_and_live_tightening(self):
        with tempfile.TemporaryDirectory(prefix='rocky-desktop-reference-') as directory:
            ref = Path(directory).resolve() / 'notes.txt'
            ref.write_text('DESKTOP_OUTSIDE_MARKER')
            client = fake_client([
                ('read_file', {'path': str(ref)}),
                ('grep', {'path': str(ref.parent), 'pattern': 'DESKTOP_OUTSIDE'}),
                ('glob', {'pattern': str(ref.parent / '*.txt')}), 'done',
                ('grep', {'path': str(ref.parent), 'pattern': 'DESKTOP_OUTSIDE'}), 'done',
            ])
            with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
                await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})
                await self.host.dispatch('turn/start', {**self.args, 'prompt': 'read reference'})
                await self.drain(allowed=False)
                results = [m['content'] for m in self.host.store.history(self.thread['id']) if m['role'] == 'tool']
                self.assertIn('DESKTOP_OUTSIDE_MARKER', results[0])
                self.assertIn('DESKTOP_OUTSIDE_MARKER', results[1])
                self.assertIn('notes.txt', results[2])
                await self.host.dispatch('mode/set', {**self.args, 'mode': 'ask'})
                await self.host.dispatch('turn/start', {**self.args, 'prompt': 'read again'})
                await self.drain(allowed=False)
                self.assertIn('[denied]', self.host.store.history(self.thread['id'])[-2]['content'])

    async def test_search_grant_is_read_only_and_revoked_on_tightening(self):
        from rockycode.engine.tools import execute
        with tempfile.TemporaryDirectory(prefix='rocky-desktop-grant-') as directory:
            ref = Path(directory).resolve() / 'notes.txt'
            ref.write_text('APPROVED_SEARCH_MARKER')
            client = fake_client([('grep', {'path': str(ref.parent), 'pattern': 'APPROVED_SEARCH'}), 'done'])
            with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
                await self.host.dispatch('turn/start', {**self.args, 'prompt': 'search reference'})
                await self.drain(decision='once')
            engine = self.host.adapters[self.thread['id']].engine
            self.assertIn(ref.parent, engine.read_grants)
            self.assertIn('APPROVED_SEARCH', self.host.store.history(self.thread['id'])[-2]['content'])
            result, _ = await execute(engine.registry, 'write_file', json.dumps({'path': str(ref), 'content': 'bad'}))
            self.assertIn('[blocked]', result)
            self.assertEqual(ref.read_text(), 'APPROVED_SEARCH_MARKER')
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'careful'})
            self.assertFalse(engine.read_grants)
            result, _ = await execute(engine.registry, 'glob', json.dumps({'pattern': str(ref.parent / '*.txt')}))
            self.assertIn('[blocked]', result)

    async def test_ready_means_a_model_can_run(self):
        from rockycode_desktop.adapter import inspect_runtime, launch_model
        kimi = {'ROCKYCODE_KIMI_API_KEY': 'fixture-kimi'}
        # The saved default works: tasks start on it.
        self.assertEqual(launch_model('fake'), 'fake')
        with hermetic_env(self.root / 'global', **kimi):
            self.assertEqual(launch_model('kimi:kimi-k3'), 'kimi:kimi-k3')
        # The saved default has no key, another provider has one: start there
        # instead of saying "Ready" and failing on the first message.
        with hermetic_env(self.root / 'global', **kimi):
            del os.environ['ROCKYCODE_API_KEY']
            self.assertEqual(launch_model('deepseek-flash'), 'kimi:kimi-k3')
            # A key and no saved default at all is enough, too.
            self.assertEqual(launch_model(''), 'kimi:kimi-k3')
            info = inspect_runtime()
            self.assertTrue(info['available'])
        # Nothing can run: not available, with the reason and the exact command.
        with hermetic_env(self.root / 'global'):
            del os.environ['ROCKYCODE_API_KEY']
            self.assertEqual(launch_model('deepseek-flash'), '')
            info = inspect_runtime()
            self.assertEqual((info['available'], info['model'], info['error_key']), (False, '', 'diagnostic.configureModel'))
            self.assertRegex(info['setup_command'], r'rockycode(\.cli)?$')
            self.assertEqual(info['keychain_silent'], [])
            self.assertNotIn('reopen', info['error'])

    async def test_a_task_whose_model_has_no_key_says_so_in_one_sentence(self):
        self.thread['model'] = 'kimi:kimi-k3'  # no Kimi key in this environment
        await self.host.dispatch('turn/start', {**self.args, 'prompt': 'hello'})
        async with asyncio.timeout(15):
            while self.host.tasks:
                await asyncio.sleep(.01)
        message = self.thread['messages'][-1]
        self.assertEqual(message['error_key'], 'diagnostic.modelNoKey')
        self.assertEqual(message['error_params'], {'model': 'kimi:kimi-k3', 'variable': 'ROCKYCODE_KIMI_API_KEY'})
        self.assertIn('Pick another model above', message['error'])
        self.assertNotIn('.env', message['error'])
        # A key the system keychain holds and did not give is said as that, not as "no key yet".
        from rockycode import onboarding
        with patch.object(onboarding, 'keychain_silent', return_value=['ROCKYCODE_KIMI_API_KEY']):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'again'})
            async with asyncio.timeout(15):
                while self.host.tasks:
                    await asyncio.sleep(.01)
        message = self.thread['messages'][-1]
        self.assertEqual(message['error_key'], 'diagnostic.modelKeychainSilent')
        self.assertIn('the keychain did not answer', message['error'])

    async def test_a_key_is_added_from_inside_the_app(self):
        from rockycode import onboarding
        with patch.object(onboarding, 'keychain_available', return_value=False):  # never the developer's keychain
            listing = await self.host.dispatch('setup/providers', {})
        rows = {row['id']: row for row in listing['providers']}
        self.assertIs(listing['keychain'], False)
        self.assertTrue(rows['deepseek']['configured'])  # the fixture home key
        self.assertEqual(rows['deepseek']['place'], 'shell', 'an export, as far as the store knows')
        self.assertEqual((rows['kimi']['configured'], rows['kimi']['host'], rows['kimi']['place']),
                         (False, 'api.moonshot.cn', ''))
        self.assertIn('qwen-plan', rows)
        self.assertNotIn('ollama', rows, 'a local server takes no key')
        saved = []

        def save(*args, **kwargs):  # never the real saver: this process may point at the developer's key file
            saved.append((args, kwargs))
            return '~/.rockycode/.env'

        with patch.object(onboarding, 'save_key', save):
            with patch.object(onboarding, 'probe_key', return_value='rejected') as probe:
                with self.assertRaises(Exception) as refused:
                    await self.host.dispatch('setup/saveKey', {'endpoint': 'kimi', 'key': 'k-typo'})
                self.assertEqual(refused.exception.key, 'diagnostic.keyRejected')
                probe.assert_called_once_with('k-typo', 'https://api.moonshot.cn/v1')
            with patch.object(onboarding, 'probe_key', return_value='ok') as probe:
                for bad in ({'endpoint': 'nope', 'key': 'k'}, {'endpoint': 'ollama', 'key': 'k'},
                            {'endpoint': 'kimi', 'key': '   '}, {'endpoint': 'kimi'}, {'key': 'k'}):
                    with self.assertRaises(Exception):
                        await self.host.dispatch('setup/saveKey', bad)
                probe.assert_not_called()
                self.assertEqual(saved, [], 'a rejected or malformed request saves nothing')
                result = await self.host.dispatch('setup/saveKey', {'endpoint': 'kimi', 'key': '  k-kimi-new  '})
            with patch.object(onboarding, 'probe_key', return_value='unverified'):
                offline = await self.host.dispatch('setup/saveKey', {'endpoint': 'deepseek', 'key': 'k-home-new'})
            with patch.object(onboarding, 'probe_key', return_value='ok'):
                with patch.object(onboarding, 'keychain_available', return_value=True):
                    self.assertIs((await self.host.dispatch('setup/providers', {}))['keychain'], True)
        # The form asked for the system keychain and it did not take the key: nothing is saved, and
        # the form says so. The key file is the user's own choice, by unticking the box.
        with patch.object(onboarding, 'save_key', side_effect=onboarding.KeychainRefused('ROCKYCODE_KIMI_API_KEY')), \
                patch.object(onboarding, 'probe_key', return_value='ok'):
            with self.assertRaises(Exception) as refused:
                await self.host.dispatch('setup/saveKey', {'endpoint': 'kimi', 'key': 'k-kimi-new', 'keychain': True})
        self.assertEqual(refused.exception.key, 'diagnostic.keychainRefused')
        self.assertIn('nothing was saved', str(refused.exception))
        self.assertNotIn('k-kimi-new', str(refused.exception))
        with patch.object(onboarding, 'save_key', return_value=onboarding.KEYCHAIN) as kept_safe, \
                patch.object(onboarding, 'probe_key', return_value='ok'):
            safe = await self.host.dispatch('setup/saveKey', {'endpoint': 'kimi', 'key': 'k-kimi-new', 'keychain': True})
            await self.host.dispatch('setup/saveKey', {'endpoint': 'kimi', 'key': 'k-kimi-new', 'keychain': 'yes'})
        self.assertEqual([call.kwargs for call in kept_safe.call_args_list],
                         [{'prefer_keyring': True}, {'prefer_keyring': False}], 'only a real true asks for the keychain')
        self.assertEqual(saved, [(('ROCKYCODE_KIMI_API_KEY', 'k-kimi-new'), {'prefer_keyring': False}),
                                 (('ROCKYCODE_API_KEY', 'k-home-new'), {'prefer_keyring': False})])
        self.assertEqual((result['verified'], result['runtime']['available'], offline['verified']), (True, True, False))
        self.assertEqual((result['place'], safe['place']), ('file', 'keychain'))
        self.assertNotIn('refused', result, 'a save either lands where it was asked to, or is an error')
        # The key passes through and is gone: not in the answer, not in what the host keeps.
        kept = json.dumps([result, offline, safe, self.host.snapshot()], default=str)
        self.assertNotIn('k-kimi-new', kept)
        self.assertNotIn('k-home-new', kept)

    async def test_a_failed_model_request_is_one_translated_sentence(self):
        import httpx
        import openai
        request = httpx.Request('POST', 'https://api.deepseek.com/v1/chat/completions')
        body = {'error': {'message': 'Authentication Fails', 'type': 'authentication_error'}}
        rejected = openai.AuthenticationError(f'Error code: 401 - {body}', response=httpx.Response(401, request=request), body=body)

        async def create(**kw):
            raise rejected

        client = NS(chat=NS(completions=NS(create=create)), close=AsyncMock())
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'hello'})
            async with asyncio.timeout(15):
                while self.host.tasks:
                    await asyncio.sleep(.01)
        message = self.thread['messages'][-1]
        # The bubble used to show the SDK's text: a class name and a dict.
        self.assertEqual((message['status'], message['error_key']), ('error', 'diagnostic.requestAuth'))
        self.assertEqual((message['error_params']['status'], message['error_params']['host']), (401, 'api.deepseek.com'))
        self.assertIn('The key was rejected (401)', message['error'])
        self.assertNotIn('authentication_error', message['error'])
        self.assertNotIn('fixture-home', json.dumps(message), 'never the key')

    async def test_new_tasks_honour_a_careful_config_and_never_loosen(self):
        from rockycode import config
        mode = lambda: self.host.create_thread(str(self.root))['permissionMode']  # noqa: E731
        self.assertEqual(mode(), 'ask')
        with patch.object(config, 'GLOBAL_PATH', self.root / 'global' / 'config.toml'):
            config.GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.GLOBAL_PATH.write_text('permission = "careful"\n')
            self.assertEqual(mode(), 'careful')
            config.GLOBAL_PATH.write_text('permission = "yolo"\n')
            self.assertEqual(mode(), 'ask', 'a config never starts a desktop task in auto-approve')
            config.GLOBAL_PATH.write_text('permission = "ask"\n')
            project = self.root / '.rockycode' / 'config.toml'
            project.parent.mkdir(exist_ok=True)
            project.write_text('permission = "careful"\n')
            self.assertEqual(mode(), 'careful', 'a project may tighten')

    async def test_model_switch_rebuilds_only_this_task_and_uses_its_model(self):
        listed = await self.host.dispatch('models/list', {})
        target = next(m['id'] for m in listed['models'] if m['id'].endswith(':deepseek-flash'))
        with self.assertRaises(ValueError):
            await self.host.dispatch('model/set', {**self.args, 'model': 'no-such-model'})
        client = fake_client(['done', 'again'])
        built = []
        def factory(*args):
            built.append(args[0])
            return EngineAdapter(*args, client=client, include_user=False, mcp=False, lsp=False)
        with patch('rockycode_desktop.host.EngineAdapter', factory):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'warm'})
            await self.drain()
            await self.host.dispatch('model/set', {**self.args, 'model': target})
            self.assertEqual(self.thread['model'], target)
            self.assertEqual(self.host.runtime['model'], 'fake')
            self.assertEqual(os.environ['ROCKYCODE_MODEL'], 'fake')
            self.assertNotIn(self.thread['id'], self.host.adapters)
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'use chosen model'})
            await self.drain()
        self.assertEqual(built, ['fake', target])

    async def test_artifact_creation_stays_in_desktop_and_survives_reopen(self):
        client = fake_client([('create_artifact', {'title': 'Project overview', 'html': '<h1>Project</h1><button>Interactive</button>'}), 'done'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)), patch('webbrowser.open') as browser:
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'make an overview'})
            await self.drain()
            browser.assert_not_called()
        artifact = self.thread['artifacts'][0]
        self.assertEqual(artifact['title'], 'Project overview')
        result = await self.host.dispatch('artifact/read', {**self.args, 'path': artifact['path']})
        self.assertIn('<h1>Project</h1>', result['html'])
        reopened = Host(str(self.root / 'data'), fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['artifacts'], [artifact])
        await reopened.close()

    async def test_a_task_lists_its_own_artifacts_one_entry_each(self):
        replies = [('create_artifact', {'title': 'Project overview', 'html': '<h1>first</h1>'}), 'done']
        client = fake_client(replies)
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)), patch('webbrowser.open'):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'make an overview'})
            await self.drain()
            first = dict(self.thread['artifacts'][0])
            # A new version is a new file. It takes the artifact's entry instead of adding one.
            replies += [('create_artifact', {'title': 'Project overview', 'html': '<h1>second</h1>',
                                             'artifact_id': first['id'], 'base_revision': 1}), 'done']
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'revise it'})
            await self.drain()
            self.assertEqual(len(self.thread['artifacts']), 1)
            latest = self.thread['artifacts'][0]
            self.assertEqual(latest['id'], first['id'])
            self.assertNotEqual(latest['path'], first['path'])
            shown = await self.host.dispatch('artifact/read', {**self.args, 'path': latest['path']})
            self.assertIn('<h1>second</h1>', shown['html'])
            # Another task in the same folder does not inherit this task's artifacts.
            mine, mine_args = self.thread, self.args
            self.thread = await self.host.dispatch('thread/create', {'workspace': str(self.root)})
            self.args = {'threadId': self.thread['id']}
            replies += ['nothing to show']
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'say hi'})
            await self.drain()
            self.assertEqual(self.thread.get('artifacts', []), [])
            self.assertEqual(len(mine['artifacts']), 1)
            self.thread, self.args = mine, mine_args

    async def test_session_grant_scopes_to_the_binary(self):
        # `touch` approved for the session must cover a second `touch` without a
        # prompt, while a different binary (`git`) still asks.
        client = fake_client([('bash', {'command': 'touch one.txt'}), ('bash', {'command': 'git status'}), 'done'])
        prompts = []
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'session test'})
            async with asyncio.timeout(15):
                while self.host.tasks:
                    approval = self.thread['approval']
                    if approval:
                        prompts.append(approval['sessionLabel'])
                        respond = 'session' if len(prompts) == 1 else 'deny'
                        await self.host.dispatch('approval/respond', {**self.args, 'approvalId': approval['id'], 'decision': respond})
                    await asyncio.sleep(.01)
            self.assertEqual(self.thread['messages'][-1]['status'], 'complete', self.thread['messages'][-1].get('error'))
        self.assertEqual(prompts, ['touch', 'git'])
        self.assertTrue((self.root / 'one.txt').exists())
        self.assertIn('[denied]', self.host.store.history(self.thread['id'])[-2]['content'])
        self.assertEqual(self.host.approvers[self.thread['id']].session.bins, {'touch'})
        # The grant lives in the shared pipeline, on the engine the task runs with.
        self.assertIs(self.host.adapters[self.thread['id']].engine.approver, self.host.approvers[self.thread['id']])

    async def test_static_block_never_prompts_and_is_marked_on_the_tool_row(self):
        # A never-allowed command is refused by the pipeline in every mode — the
        # system blocking, not the user denying — so the card must not appear
        # and the tool row says why the call did not run.
        client = fake_client([('bash', {'command': 'sudo rm -rf /'}), 'done'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)), \
                patch.object(self.host, '_prompt_approval', new=AsyncMock(return_value='once')) as prompt:
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'block test'})
            await self.drain()
            prompt.assert_not_awaited()
        row = self.thread['messages'][-1]['tools'][0]
        self.assertEqual((row['name'], row['status'], row['decision']), ('bash', 'error', 'blocked'))
        self.assertEqual(row['detail'], 'sudo rm -rf /', 'a blocked call still shows the command that was refused')
        self.assertIn('[denied]', self.host.store.history(self.thread['id'])[-2]['content'])
        approver = self.host.approvers[self.thread['id']]
        self.assertEqual(approver.refused[0]['command'], 'sudo rm -rf /')
        self.assertEqual(approver.mode, 'yolo')

    async def test_mode_changes_reach_the_running_task_and_tightening_revokes_grants(self):
        client = fake_client([('bash', {'command': 'touch keep.txt'}), 'done'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'grant'})
            await self.drain(decision='session')
        approver = self.host.approvers[self.thread['id']]
        self.assertEqual(approver.session.bins, {'touch'})
        await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})  # loosening keeps grants
        self.assertEqual((approver.mode, approver.session.bins), ('yolo', {'touch'}))
        await self.host.dispatch('mode/set', {**self.args, 'mode': 'careful'})  # tightening revokes
        self.assertEqual((approver.mode, approver.session.bins), ('careful', set()))

    async def test_approval_redaction_preserves_original_args(self):
        secret = 'test-secret-with-"quotes"-and-backslash\\end'
        data = {'nested': [{'value': secret}]}
        with patch.dict(os.environ, {'ROCKYCODE_TEST_API_KEY': secret}):
            task = asyncio.create_task(self.host.approve(self.thread, 'probe', data))
            await asyncio.sleep(0)
            approval = self.thread['approval']
            self.assertEqual(approval['args']['nested'][0]['value'], '[redacted]')
            self.assertEqual(data['nested'][0]['value'], secret)
            await self.host.dispatch('approval/respond', {**self.args, 'approvalId': approval['id'], 'allowed': False})
            self.assertFalse(await task)

    # yolo never waits. A step only the user can allow (here: a shell command that names
    # rocky's own folder — harmless, the home is this test's throwaway one) is refused at
    # once and kept in thread["parked"]; the card with its five-minute timer never shows.
    PARKED = {'command': 'echo "$ROCKYCODE_HOME" > picked.txt'}

    async def park_one(self, *more):
        client = self.client = fake_client([('bash', self.PARKED), ('bash', {'command': 'touch rest.txt'}), 'one step waits', *more])
        self.adapter_patch = patch('rockycode_desktop.host.EngineAdapter', self.factory(client))
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)
        with patch.object(self.host, '_prompt_approval', new=AsyncMock(return_value='once')) as prompt:
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'long task'})
            await self.drain()
            prompt.assert_not_awaited()
        return self.host.approvers[self.thread['id']]

    async def test_yolo_parks_a_step_only_the_user_can_allow_and_runs_it_on_their_word(self):
        approver = await self.park_one(('bash', self.PARKED), 'picked up')
        self.assertTrue((self.root / 'rest.txt').exists(), 'the rest of the turn ran')
        self.assertFalse((self.root / 'picked.txt').exists(), 'the parked step did not')
        row = self.thread['messages'][-1]['tools'][0]
        self.assertEqual((row['name'], row['decision']), ('bash', 'parked'))
        history = self.host.store.history(self.thread['id'])
        refusal = next(m['content'] for m in history if m['role'] == 'tool')
        self.assertTrue(refusal.startswith('[parked]') and 'another way' in refusal, refusal)
        step, = self.thread['parked']
        self.assertEqual((step['tool'], step['detail']), ('bash', self.PARKED['command']))
        self.assertTrue(step['warning'] and step['id'] == approver.parked[0].id and 'args' not in step)
        # Run it now: a pass for exactly that call; the client's sentence tells rocky to pick it up.
        result = await self.host.dispatch('parked/decide', {**self.args, 'stepId': step['id'], 'decision': 'run',
                                                            'prompt': 'I allow the step you parked.\n' + step['detail']})
        self.assertEqual(result, {'parked': 0})
        await self.drain()
        self.assertTrue((self.root / 'picked.txt').exists(), 'the allowed step ran')
        self.assertEqual((self.thread['parked'], approver.parked, approver.once, self.host.pickups), ([], [], set(), {}))
        self.assertEqual(self.thread['messages'][-2]['content'].splitlines()[0], 'I allow the step you parked.')
        with self.assertRaisesRegex(Exception, 'no longer waiting'):
            await self.host.dispatch('parked/decide', {**self.args, 'stepId': step['id'], 'decision': 'run', 'prompt': 'again'})

    async def test_a_pick_up_taken_out_of_the_queue_allows_nothing_and_its_step_waits_again(self):
        approver = await self.park_one('never')
        step, = self.thread['parked']
        scripted, hold = self.client.chat.completions.create, asyncio.Event()

        async def create(**kw):
            await hold.wait()  # the model never answers: the turn is running until it is stopped
            return await scripted(**kw)

        self.client.chat.completions.create = create
        await self.host.dispatch('turn/start', {**self.args, 'prompt': 'long work'})
        result = await self.host.dispatch('parked/decide', {**self.args, 'stepId': step['id'], 'decision': 'run',
                                                            'prompt': 'I allow the step you parked.'})
        self.assertEqual((result, len(self.thread['delivery']['pending']), approver.once), ({'parked': 0}, 1, set()))
        await self.host.dispatch('turn/clear', self.args)
        self.assertEqual((self.host.pickups, approver.once, [p.id for p in approver.parked]), ({}, set(), [step['id']]))
        self.assertEqual([row['id'] for row in self.thread['parked']], [step['id']], 'the step waits again')
        await self.host.dispatch('turn/cancel', self.args)
        async with asyncio.timeout(15):
            while self.host.tasks:
                await asyncio.sleep(.01)
        self.assertFalse((self.root / 'picked.txt').exists())

    async def test_a_skipped_parked_step_runs_nothing_and_rocky_is_told(self):
        approver = await self.park_one()
        step, = self.thread['parked']
        self.assertEqual(await self.host.dispatch('parked/decide', {**self.args, 'stepId': step['id'], 'decision': 'skip'}),
                         {'parked': 0})
        self.assertEqual((self.thread['parked'], approver.parked, approver.once, self.host.tasks), ([], [], set(), {}))
        self.assertFalse((self.root / 'picked.txt').exists())
        note = self.host.store.history(self.thread['id'])[-1]
        self.assertEqual(note['role'], 'user')
        self.assertIn('decided not to run this parked step: bash echo', note['content'])
        with self.assertRaisesRegex(Exception, 'invalid|Invalid'):
            await self.host.dispatch('parked/decide', {**self.args, 'stepId': step['id'], 'decision': 'always'})

    async def test_the_third_parked_step_stops_the_turn_and_holds_the_queue(self):
        reach = [('bash', {'command': f'echo {ref} > picked.txt'}) for ref in ('$ROCKYCODE_HOME', '${ROCKYCODE_HOME}', '"$ROCKYCODE_HOME"/')]
        client = fake_client([*reach, 'never requested'])
        scripted, queued = client.chat.completions.create, asyncio.Event()

        async def create(**kw):
            await queued.wait()  # the model answers only once the second message is waiting behind the turn
            return await scripted(**kw)

        client.chat.completions.create = create
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'yolo'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'look around'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'queued behind it', 'delivery': 'queue'})
            queued.set()
            await self.drain()
        last = self.thread['messages'][-1]
        self.assertEqual((last['error_key'], last['error_params']), ('diagnostic.parkStopped', {'count': 3}))
        self.assertEqual(len(self.thread['parked']), 3)
        self.assertFalse((self.root / 'picked.txt').exists())
        self.assertEqual((self.thread['delivery']['paused'], len(self.thread['delivery']['pending'])), (True, 1),
                         'a stopped turn holds the queued message for the user')
        unused = await anext(await scripted())
        self.assertEqual(unused.choices[0].delta.content, 'never requested', 'the model is not asked again after the stop')

    async def test_parked_steps_do_not_outlive_the_app_run_and_ask_mode_still_asks(self):
        await self.park_one()
        self.assertEqual(len(self.thread['parked']), 1)
        await self.host.close()
        self.host = Host(str(self.root / 'data'))
        self.thread = self.host.thread(self.thread['id'])
        self.assertEqual(self.thread['parked'], [], 'the exact call lived in the process that parked it')
        # ask mode: the same step comes as a card at once; nothing is parked.
        self.adapter_patch.stop()
        client = fake_client([('bash', self.PARKED), 'asked'])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('mode/set', {**self.args, 'mode': 'ask'})
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'ask mode'})
            asked = []
            async with asyncio.timeout(15):
                while self.host.tasks:
                    approval = self.thread['approval']
                    if approval:
                        asked.append((approval['sessionLabel'], bool(approval.get('warning'))))
                        await self.host.dispatch('approval/respond', {**self.args, 'approvalId': approval['id'], 'decision': 'deny'})
                    await asyncio.sleep(.01)
        self.assertEqual((asked, self.thread['parked']), ([(None, True)], []))

    async def test_cancel_actual_process_and_resume_after_restart(self):
        # Real harmless process; fake model selects the tool deterministically.
        client = fake_client([('bash', {'command': 'echo $$ > child.pid; exec sleep 60'})])
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(client)):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'sleep'})
            task = self.host.tasks[self.thread['id']]
            async with asyncio.timeout(10):
                while not (self.root / 'child.pid').exists():
                    approval = self.thread['approval']
                    if approval:
                        await self.host.dispatch('approval/respond', {**self.args, 'approvalId': approval['id'], 'allowed': True})
                    await asyncio.sleep(.01)
            pid = int((self.root / 'child.pid').read_text())
            await self.host.dispatch('turn/cancel', self.args)
            await asyncio.wait_for(task, 15)
        self.assertEqual(self.thread['messages'][-1]['status'], 'cancelled')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.host.adapters)
        history = self.host.store.history(self.thread['id'])
        self.assertIn('interrupted', history[-1]['content'])
        await self.host.close()
        self.host = Host(str(self.root / 'data'))
        self.thread = self.host.threads[self.args['threadId']]
        with patch('rockycode_desktop.host.EngineAdapter', self.factory(fake_client(['resumed']))):
            await self.host.dispatch('turn/start', {**self.args, 'prompt': 'continue'})
            await self.drain()
        self.assertEqual(self.thread['messages'][-1]['content'], 'resumed')
