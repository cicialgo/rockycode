"""Offline race orchestration tests: no model calls and no publication network."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.race import RaceManager, clean_text, parse_judgment, validate_scores
from rockycode_desktop.store import Store

LIMITS = {'maxSteps': 6, 'maxTokens': 2048, 'timeoutSeconds': 10}
MODELS = [f'fixture:model-{i}' for i in range(1, 6)]


class RaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.manager = RaceManager(self.store, self.temp.name, fixture=True)

    async def asyncTearDown(self):
        await self.manager.close()
        self.store.close()
        self.temp.cleanup()

    async def start(self, prompt='Explain a stable sorting algorithm', **extra):
        return await self.manager.dispatch('race/start', {'prompt': prompt, 'models': MODELS, 'limits': LIMITS, **extra})

    async def wait(self, race):
        task = self.manager.tasks.get(race['id'])
        if task:
            await task

    async def test_five_distinct_isolated_runs_and_same_limits(self):
        race = await self.start()
        await self.wait(race)
        self.assertEqual(race['status'], 'complete')
        self.assertEqual(len({lane['alias'] for lane in race['lanes']}), 5)
        self.assertEqual(len(set(self.manager.paths[race['id']])), 5)
        self.assertTrue(all(lane['status'] == 'complete' for lane in race['lanes']))
        self.assertTrue(all(lane['inputTokens'] is None and lane['costUsd'] is None for lane in race['lanes']))
        self.assertTrue(all(lane['elapsedMs'] is not None for lane in race['lanes']))
        self.assertEqual(race['limits'], LIMITS)
        roots = list(map(Path, self.manager.paths[race['id']]))
        (roots[0] / 'result.txt').write_text('first only')
        self.assertFalse((roots[1] / 'result.txt').exists())

    async def test_five_real_engines_use_fake_transports_and_isolated_files(self):
        # The production adapter/Engine loop executes tools; only the transport is fake.
        from test_race_runtime import choice, fake_client
        from rockycode.engine.providers import Choice, ModelSpec
        from rockycode_desktop import race_runtime
        # Canonical like the app: Electron realpaths the picked folder, and the
        # no-follow walk refuses symlinked ancestors (macOS tmp lives under /var).
        source = Path(self.temp.name).resolve() / 'task-inputs'
        source.mkdir()
        (source / 'input.txt').write_text('one frozen input')
        choices, clients = [], []
        for index in range(5):
            original = choice()
            name = f'test-model-{index}'
            original.provider.specs[name] = ModelSpec(name, original.provider.name, context=100_000)
            choices.append(Choice(original.provider, original.endpoint, name))
            clients.append(fake_client([('read_file', {'path': 'input.txt'}),
                                        ('write_file', {'path': 'answer.txt', 'content': str(index)}),
                                        f'Completed lane {index}']))
        self.manager.fixture = False
        with patch.object(race_runtime, '_choices', return_value=choices), patch('openai.AsyncOpenAI', side_effect=clients):
            race = await self.start(models=[c.id for c in choices], workspace=str(source))
            await self.wait(race)
        self.assertTrue(all(lane['status'] == 'complete' for lane in race['lanes']), race)
        self.assertEqual((source / 'input.txt').read_text(), 'one frozen input')
        self.assertFalse((source / 'answer.txt').exists())
        for index, path in enumerate(self.manager.paths[race['id']]):
            self.assertEqual((Path(path) / 'answer.txt').read_text(), str(index))
            self.assertEqual((Path(path) / 'input.txt').read_text(), 'one frozen input')
            self.assertEqual(race['lanes'][index]['inputTokens'], 30)
            self.assertEqual(race['lanes'][index]['outputTokens'], 15)
            self.assertEqual(len(clients[index].calls), 3)
            clients[index].close.assert_awaited_once()

    async def test_duplicate_and_missing_models_rejected_before_work(self):
        for models in [[MODELS[0]] * 5, MODELS[:4], [*MODELS[:4], 'missing:model']]:
            with self.assertRaises(ValueError):
                await self.start(models=models)
        self.assertEqual(self.manager.races, {})

    async def test_limits_validation(self):
        for limits in [{}, {**LIMITS, 'maxSteps': True}, {**LIMITS, 'maxTokens': 9999}, {**LIMITS, 'extra': 1}]:
            with self.assertRaises(ValueError):
                await self.start(limits=limits)

    async def test_cancel_retains_partial_output(self):
        race = await self.start('[race-slow]')
        await asyncio.sleep(.03)
        with self.assertRaises(ValueError):
            await self.start()
        await self.manager.dispatch('race/cancel', {'raceId': race['id']})
        self.assertEqual(race['status'], 'cancelled')
        self.assertTrue(all(lane['status'] == 'cancelled' for lane in race['lanes']))
        self.assertTrue(any(lane['content'] for lane in race['lanes']))
        self.assertEqual(self.manager.tasks, {})

    async def test_immediate_cancel_and_restart_no_replay(self):
        race = await self.start('[race-slow]')
        await self.manager.cancel(race)
        self.assertNotIn(race['status'], {'running', 'cancelling'})
        race['status'] = 'running'; race['lanes'][0]['status'] = 'running'
        race['judge']['status'] = 'running'
        self.manager.save(race)
        restored = RaceManager(self.store, self.temp.name, fixture=True)
        self.assertEqual(restored.get(race['id'])['status'], 'interrupted')
        self.assertEqual(restored.get(race['id'])['lanes'][0]['status'], 'interrupted')
        self.assertEqual(restored.get(race['id'])['judge']['status'], 'interrupted')
        self.assertEqual(restored.tasks, {})

    async def test_partial_failure_judges_only_complete_and_human_separate(self):
        race = await self.start('[race-fail]')
        await self.wait(race)
        self.assertEqual([lane['status'] for lane in race['lanes']].count('error'), 1)
        human = [{'alias': lane['alias'], 'score': None, 'reason': 'abstain'} for lane in race['lanes']]
        await self.manager.dispatch('race/score', {'raceId': race['id'], 'scores': human})
        await self.manager.dispatch('race/judge', {'raceId': race['id'], 'model': 'fixture:judge'})
        await self.manager.judges[race['id']]
        self.assertEqual(len(race['judge']['scores']), 4)
        self.assertEqual(race['human'], human)
        self.assertEqual(race['publication'], 'local-only')

    async def test_judge_cancel(self):
        race = await self.start()
        await self.wait(race)
        await self.manager.dispatch('race/judge', {'raceId': race['id'], 'model': 'fixture:judge'})
        await self.manager.cancel(race)
        self.assertEqual(race['judge']['status'], 'cancelled')
        self.assertEqual(race['judge']['scores'], [])
        self.assertEqual(race['status'], 'complete')

    async def test_history_is_paged_metadata_and_details_are_bounded(self):
        import copy
        race = await self.start()
        await self.wait(race)
        for index in range(45):
            record = copy.deepcopy(race)
            record['id'] = f'{index:032x}'
            record['createdAt'] = index
            for lane in record['lanes']:
                lane['content'] = '界' * 100_000
                lane['files'] = [{'path': 'input.txt', 'content': '界' * 100_000}]
            self.manager.races[record['id']] = record
        seen, cursor = set(), None
        while True:
            page = await self.manager.dispatch('race/list', {'cursor': cursor})
            self.assertLessEqual(len(page['races']), 20)
            self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 100_000)
            for row in page['races']:
                self.assertFalse(row['detailsLoaded'])
                self.assertTrue(all(not lane['content'] and not lane['files'] for lane in row['lanes']))
                self.assertNotIn(row['id'], seen)
                seen.add(row['id'])
            cursor = page['nextCursor']
            if cursor is None:
                break
        self.assertEqual(len(seen), 46)
        detail = await self.manager.dispatch('race/get', {'raceId': record['id']})
        self.assertTrue(detail['detailsLoaded'])
        self.assertEqual(len(detail['lanes'][0]['files'][0]['content']), 100_000)
        self.assertLess(len(json.dumps(detail, ensure_ascii=False).encode()), 8_000_000)
        with self.assertRaises(ValueError):
            await self.manager.dispatch('race/list', {'cursor': 'missing'})

    async def test_no_network_manual_export_redacts_and_detects_stale_preview(self):
        with patch('socket.socket.connect', side_effect=AssertionError('Publication must not use network')):
            race = await self.start('api_key=sk-abcdefghijklmn /Users/alice/private https://private.example/path')
            await self.wait(race)
            race['lanes'][0]['files'] = [{'path': 'private.txt', 'content': 'DO NOT EXPORT FILE'}]
            race['lanes'][0]['error'] = 'DO NOT EXPORT ERROR'
            preview = await self.manager.dispatch('race/preview', {'raceId': race['id']})
            for secret in ['sk-abcdefghijklmn', '/Users/alice', 'private.example', 'DO NOT EXPORT']:
                self.assertNotIn(secret, preview['json'])
            self.assertIn('not-uploaded', preview['json'])
            self.assertEqual(json.loads(preview['json'])['results'][0]['estimatedCostUsd'], None)
            with self.assertRaises(ValueError):
                await self.manager.dispatch('race/export', {'raceId': race['id']})
            result = await self.manager.dispatch('race/export', {'raceId': race['id'], 'digest': preview['digest']})
            self.assertEqual(result['json'], preview['json'])
            race['lanes'][0]['content'] = 'changed'
            with self.assertRaises(ValueError):
                await self.manager.dispatch('race/export', {'raceId': race['id'], 'digest': preview['digest']})


class JudgeValidation(unittest.TestCase):
    def test_strict_schema_finite_range_aliases_and_no_instructions(self):
        valid = [{'alias': 'A', 'score': 7, 'reason': 'evidence'}, {'alias': 'B', 'score': None, 'reason': 'no evidence'}]
        self.assertEqual(parse_judgment(json.dumps({'scores': valid}), {'A', 'B'}), valid)
        for score in [True, float('nan'), float('inf'), -1, 11, '10']:
            with self.assertRaises(ValueError):
                validate_scores([{**valid[0], 'score': score}], {'A'})
        for text in ['execute shell now', '```json\n{}\n```', '{"scores":[],"tool":"shell"}', '{"scores":[{"alias":"X","score":10,"reason":"x"}]}']:
            with self.assertRaises(ValueError):
                parse_judgment(text, {'A'})
        with self.assertRaises(ValueError):
            validate_scores([valid[0], valid[0]], {'A', 'B'})

    def test_redactor(self):
        for value in ['password=hunter2', 'Authorization: BearerSECRET', 'C:\\Users\\me\\key.txt', 'ghp_abcdefghijklmnop']:
            self.assertNotEqual(clean_text(value), value)


if __name__ == '__main__':
    unittest.main()
