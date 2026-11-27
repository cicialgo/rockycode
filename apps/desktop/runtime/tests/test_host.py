import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.host import Host


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []
        self.host = Host(self.directory.name, fixture=True, emit=lambda event: self.events.append(event.copy()))
        self.thread = await self.host.dispatch('thread/create', {'workspace':self.directory.name})
        self.args = {'threadId':self.thread['id']}

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def test_only_one_run_and_cancel_has_terminal_state(self):
        await self.host.dispatch('turn/start', {**self.args,'prompt':'[long]'})
        await asyncio.sleep(.03)
        with self.assertRaises(ValueError):
            await self.host.dispatch('turn/start', {**self.args,'prompt':'overlap'})
        task=self.host.tasks[self.thread['id']]
        await self.host.dispatch('turn/cancel', self.args)
        await task
        self.assertEqual(self.thread['messages'][-1]['status'],'cancelled')
        self.assertEqual(self.thread['status'],'idle')
        self.assertNotIn(self.thread['id'],self.host.tasks)

    async def test_immediate_stop_always_cleans_run(self):
        await self.host.dispatch('turn/start', {**self.args, 'prompt':'[long]'})
        task = self.host.tasks[self.thread['id']]
        await self.host.dispatch('turn/cancel', self.args)
        await task
        self.assertEqual(self.thread['messages'][-1]['status'], 'cancelled')
        self.assertFalse(self.host.tasks)

    async def test_notes_drafts_and_transcript_survive_reopen(self):
        await self.host.dispatch('note/save',{**self.args,'content':'用户的原稿'})
        await self.host.dispatch('draft/save',{**self.args,'content':'还没发送'})
        reopened=Host(self.directory.name,fixture=True)
        saved=reopened.threads[self.thread['id']]
        self.assertEqual(saved['note'],'用户的原稿')
        self.assertEqual(saved['draft'],'还没发送')
        await reopened.close()

    async def test_interrupted_runs_never_replay_on_restart(self):
        self.thread['status']='running'
        self.thread['messages']=[{'id':'m','status':'streaming','content':'已有内容','tools':[{'id':'t','status':'running'}]}]
        self.host.save(self.thread)
        reopened=Host(self.directory.name,fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['status'],'interrupted')
        self.assertEqual(reopened.threads[self.thread['id']]['messages'][0]['content'],'已有内容')
        self.assertFalse(reopened.tasks)
        self.assertEqual(reopened.threads[self.thread['id']]['messages'][0]['tools'][0]['status'], 'interrupted')
        await reopened.close()

    async def test_approval_is_bound_to_request_and_can_be_denied(self):
        await self.host.dispatch('turn/start',{**self.args,'prompt':'[approval]'})
        await asyncio.sleep(.03)
        with self.assertRaises(ValueError):
            await self.host.dispatch('approval/respond',{**self.args,'approvalId':'wrong','decision':'once'})
        approval=self.thread['approval']['id']
        with self.assertRaises(ValueError):
            await self.host.dispatch('approval/respond',{**self.args,'approvalId':approval,'decision':'maybe'})
        self.assertIsNone(self.thread['approval']['sessionLabel'])  # fixture path offers no session grant
        await self.host.dispatch('approval/respond',{**self.args,'approvalId':approval,'decision':'deny'})
        await self.host.tasks[self.thread['id']]
        self.assertIn('拒绝',self.thread['messages'][-1]['content'])
        self.assertIsNone(self.thread['approval'])

    async def test_cancel_during_approval_cleans_pending_future(self):
        await self.host.dispatch('turn/start',{**self.args,'prompt':'[approval]'})
        await asyncio.sleep(.02)
        task=self.host.tasks[self.thread['id']]
        await self.host.dispatch('turn/cancel',self.args)
        await task
        self.assertFalse(self.host.approvals)
        self.assertIsNone(self.thread['approval'])

    async def test_invalid_workspace_and_content_rejected(self):
        with self.assertRaises(ValueError):
            await self.host.dispatch('thread/create',{'workspace':str(Path(self.directory.name)/'missing')})
        with self.assertRaises(ValueError):
            await self.host.dispatch('turn/start',{**self.args,'prompt':'   '})

    async def test_permission_mode_validated_and_defaulted(self):
        self.assertEqual(self.thread['permissionMode'],'ask')
        with self.assertRaises(ValueError):
            await self.host.dispatch('mode/set',{**self.args,'mode':'turbo'})
        self.assertEqual(self.thread['permissionMode'],'ask')
        await self.host.dispatch('mode/set',{**self.args,'mode':'yolo'})
        self.assertEqual(self.thread['permissionMode'],'yolo')
        reopened=Host(self.directory.name,fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['permissionMode'],'yolo')
        await reopened.close()

    async def test_task_models_are_independent_persistent_and_do_not_write_defaults(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        other = await self.host.dispatch('thread/create', {'workspace': self.directory.name})
        old = self.thread['model']
        choices = (await self.host.dispatch('models/list', {}))['models']
        self.assertTrue(choices)
        adapter = SimpleNamespace(close=AsyncMock())
        other_adapter = SimpleNamespace(close=AsyncMock())
        self.host.adapters.update({self.thread['id']: adapter, other['id']: other_adapter})
        other['status'] = 'running'
        with patch.dict('os.environ', {}, clear=False):
            import os
            before = os.environ.get('ROCKYCODE_MODEL')
            await self.host.dispatch('model/set', {**self.args, 'model': 'fixture:reasoning'})
            self.assertEqual(os.environ.get('ROCKYCODE_MODEL'), before)
        self.assertEqual(self.thread['model'], 'fixture:reasoning')
        self.assertEqual(other['model'], old)
        self.assertEqual(self.host.runtime['model'], old)
        adapter.close.assert_awaited_once()
        other_adapter.close.assert_not_awaited()
        with self.assertRaises(ValueError):
            await self.host.dispatch('model/set', {'threadId': other['id'], 'model': 'fixture:fast'})
        with self.assertRaises(ValueError):
            await self.host.dispatch('model/set', {**self.args, 'model': 'unknown'})
        reopened = Host(self.directory.name, fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['model'], 'fixture:reasoning')
        await reopened.close()

    async def test_legacy_task_model_pinned_on_first_open(self):
        self.thread.pop('model')
        self.host.save(self.thread)
        reopened = Host(self.directory.name, fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['model'], self.host.runtime['model'])
        self.assertIn('model', reopened.store.all()[0])
        await reopened.close()

    async def test_external_service_consent_is_exact_scoped_and_separate_from_mode(self):
        from unittest.mock import AsyncMock, patch
        args = {'server': 'docs', 'command': 'docs-mcp', 'args': [], 'source': '~/.codex/config.toml', 'configFingerprint': 'first'}
        other = await self.host.dispatch('thread/create', {'workspace': self.directory.name})
        self.thread['permissionMode'] = 'yolo'
        with patch.object(self.host, '_prompt_approval', new=AsyncMock(return_value='session')) as prompt:
            self.assertTrue(await self.host.approve(self.thread, 'runtime.start_mcp', args))
            prompt.assert_awaited_once()
            self.assertTrue(await self.host.approve(other, 'runtime.start_mcp', args))
            prompt.assert_awaited_once()  # same workspace and exact config across tasks
            self.assertTrue(await self.host.approve(other, 'runtime.start_mcp', {**args, 'server': 'different'}))
            self.assertEqual(prompt.await_count, 2)
            self.assertTrue(await self.host.approve(other, 'runtime.start_mcp', {**args, 'configFingerprint': 'changed-env'}))
            self.assertEqual(prompt.await_count, 3)
            other['workspace'] += '/another'
            self.assertTrue(await self.host.approve(other, 'runtime.start_mcp', args))
            self.assertEqual(prompt.await_count, 4)
        reopened = Host(self.directory.name, fixture=True)
        self.assertFalse(reopened.startup_grants)
        await reopened.close()

    async def test_service_skip_and_connect_once_are_not_remembered(self):
        from unittest.mock import AsyncMock, patch
        for result, allowed in [('deny', False), ('once', True)]:
            with patch.object(self.host, '_prompt_approval', new=AsyncMock(return_value=result)) as prompt:
                for _ in range(2):
                    self.assertEqual(await self.host.approve(self.thread, 'runtime.start_mcp', {'server': 'docs'}), allowed)
                self.assertEqual(prompt.await_count, 2)
        self.assertFalse(self.host.startup_grants)

    async def test_thread_rename_and_delete(self):
        with self.assertRaises(ValueError):
            await self.host.dispatch('thread/rename',{**self.args,'title':'   '})
        await self.host.dispatch('thread/rename',{**self.args,'title':'  调优笔记\n第二行  '})
        self.assertEqual(self.thread['title'],'调优笔记 第二行')
        reopened=Host(self.directory.name,fixture=True)
        self.assertEqual(reopened.threads[self.thread['id']]['title'],'调优笔记 第二行')
        await reopened.close()
        await self.host.dispatch('thread/delete',self.args)
        self.assertNotIn(self.thread['id'],self.host.threads)
        reopened=Host(self.directory.name,fixture=True)
        self.assertNotIn(self.thread['id'],reopened.threads)
        await reopened.close()

    async def test_running_thread_cannot_be_deleted(self):
        self.thread['status']='running'
        with self.assertRaises(ValueError):
            await self.host.dispatch('thread/delete',self.args)
        self.assertIn(self.thread['id'],self.host.threads)

    async def test_terminal_event_contains_complete_stream(self):
        await self.host.dispatch('turn/start',{**self.args,'prompt':'hello'})
        task=self.host.tasks[self.thread['id']]
        await task
        emitted=''.join(e['delta'] for e in self.events if e['type']=='delta')
        self.assertEqual(emitted,self.thread['messages'][-1]['content'])
        self.assertEqual(self.thread['messages'][-1]['status'],'complete')

if __name__=='__main__':
    unittest.main()
