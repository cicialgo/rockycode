import {test} from 'node:test';
import assert from 'node:assert/strict';
import {draftAfterApply, needsApplyConfirm, presetSavePayload, PRESET_BODY_LIMIT, PRESET_NAME_LIMIT} from '../src/shared/presets';
import {PresetStore} from '../src/renderer/preset-store';

test('applying a preset replaces the composer draft and confirms only when that would discard text', () => {
  assert.equal(draftAfterApply('先列证据。'), '先列证据。');
  assert.equal(needsApplyConfirm('', '先列证据。'), false);
  assert.equal(needsApplyConfirm('先列证据。', '先列证据。'), false);
  assert.equal(needsApplyConfirm('还在写', '先列证据。'), true);
  assert.equal(needsApplyConfirm(' ', '先列证据。'), true);
});

test('preset save payload keeps a name and body and drops everything else', () => {
  assert.deepEqual(presetSavePayload({name: '  审阅\n笔记  ', body: '  保留空格  '}), {name: '审阅 笔记', body: '  保留空格  '});
  const id = 'a'.repeat(32);
  assert.deepEqual(presetSavePayload({id, name: 'Review', body: 'Body'}), {id, name: 'Review', body: 'Body'});
  for (const name of ['', '   ', 'n'.repeat(PRESET_NAME_LIMIT + 1), 1]) {
    assert.throws(() => presetSavePayload({name, body: 'ok'}), /diagnostic\.presetName/);
  }
  assert.throws(() => presetSavePayload({name: 'ok', body: '   '}), /diagnostic\.presetBody/);
  assert.throws(() => presetSavePayload({name: 'ok', body: 'y'.repeat(PRESET_BODY_LIMIT + 1)}), /diagnostic\.presetBody/);
  assert.throws(() => presetSavePayload({id: 'not-an-id', name: 'ok', body: 'body'}), /diagnostic\.invalidRequest/);
  assert.equal('id' in presetSavePayload({name: 'ok', body: 'body'}), false);
});

test('preset store hydrates an optional snapshot list and replaces it without keeping the caller array', () => {
  const store = new PresetStore();
  const seen: number[] = [];
  store.subscribe(() => seen.push(store.get().length));
  store.hydrate(undefined);
  assert.deepEqual(store.get(), []);
  const incoming = [{id: 'a'.repeat(32), name: 'Review', body: 'Body', updatedAt: 1, apiKey: 'sk-nope'} as never];
  store.hydrate(incoming);
  assert.deepEqual(store.get(), [{id: 'a'.repeat(32), name: 'Review', body: 'Body', updatedAt: 1}]);
  assert.equal('apiKey' in store.get()[0], false);
  incoming[0] = {id: 'b'.repeat(32), name: 'Changed', body: 'Nope', updatedAt: 2} as never;
  assert.equal(store.get()[0].name, 'Review');
  assert.deepEqual(seen, [0, 1]);
});
