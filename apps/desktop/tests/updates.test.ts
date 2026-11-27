import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {acceptableLink, isNewer, parseFeed, FEED_LIMIT, NOTES_LIMIT} from '../src/shared/updates';
import {DAY, UpdateReminder, type FeedResponse, type FetchFeed} from '../src/main/updates';

function temporaryDirectory(t: {after: (cleanup: () => void) => void}) {
  const directory = mkdtempSync(join(tmpdir(), 'rocky-updates-'));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  return directory;
}

const GOOD = JSON.stringify({version: '0.2.0', url: 'https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0', notes: 'Model race export'});

function fakeFetch(body: string | (() => string), options: {ok?: boolean; length?: number; calls?: {url: string; init: unknown}[]} = {}): FetchFeed {
  return async (url, init) => {
    options.calls?.push({url, init});
    const text = typeof body === 'function' ? body() : body;
    const response: FeedResponse = {ok: options.ok ?? true, headers: {get: name => name === 'content-length' ? String(options.length ?? text.length) : null}, text: async () => text};
    return response;
  };
}

test('versions compare numerically and only parse x.y.z', () => {
  assert.equal(isNewer('0.2.0', '0.1.0'), true);
  assert.equal(isNewer('0.10.0', '0.9.3'), true);
  assert.equal(isNewer('v1.0.0', '0.99.99'), true);
  assert.equal(isNewer('0.1.0', '0.1.0'), false);
  assert.equal(isNewer('0.1.0-beta.1', '0.1.0'), false);
  assert.equal(isNewer('latest', '0.1.0'), false);
  assert.equal(isNewer('0.2.0', 'dev'), false);
  assert.equal(isNewer('1.2', '0.1.0'), false);
});

test('the download link must be https on a host we own', () => {
  assert.equal(acceptableLink('https://rockycode.ai/desktop/'), 'https://rockycode.ai/desktop/');
  assert.equal(acceptableLink('https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0'), 'https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0');
  for (const bad of ['http://rockycode.ai/desktop/', 'https://github.com/someone-else/rockycode/releases', 'https://github.com/', 'https://rockycode.ai.evil.example/', 'https://user:pw@rockycode.ai/', 'file:///etc/passwd', 'javascript:alert(1)', '', 42, null]) {
    assert.equal(acceptableLink(bad), null, String(bad));
  }
});

test('the feed is accepted only in exactly the published shape', () => {
  assert.deepEqual(parseFeed(GOOD), {version: '0.2.0', url: 'https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0', notes: 'Model race export'});
  assert.deepEqual(parseFeed(JSON.stringify({version: 'v0.3.1', url: 'https://rockycode.ai/desktop/'})), {version: '0.3.1', url: 'https://rockycode.ai/desktop/'});
  for (const bad of ['', '{', '[]', 'null', '"0.2.0"', JSON.stringify({version: '0.2.0'}), JSON.stringify({url: 'https://rockycode.ai/'}),
    JSON.stringify({version: 'next', url: 'https://rockycode.ai/'}), JSON.stringify({version: '0.2.0', url: 'http://rockycode.ai/'}),
    'x'.repeat(FEED_LIMIT + 1)]) {
    assert.equal(parseFeed(bad), null, bad.slice(0, 40));
  }
  const noisy = parseFeed(JSON.stringify({version: '0.2.0', url: 'https://rockycode.ai/', notes: ' <b>hi</b>\u0007\n' + 'n'.repeat(NOTES_LIMIT + 50)}));
  assert.ok(noisy?.notes);
  assert.equal(noisy.notes.length, NOTES_LIMIT);
  assert.ok(noisy.notes.startsWith('<b>hi</b>'), 'notes stay text; the renderer never treats them as markup');
  assert.ok(!noisy.notes.includes('\u0007'));
  assert.equal(parseFeed(JSON.stringify({version: '0.2.0', url: 'https://rockycode.ai/', notes: 7}))?.notes, undefined);
});

test('a newer version is reported once a day, a skipped version stays quiet, manual checks ignore both', async t => {
  const directory = temporaryDirectory(t);
  const calls: {url: string; init: unknown}[] = [];
  let clock = 1_000_000;
  const reminder = new UpdateReminder(directory, '0.1.0', 'https://rockycode.ai/desktop/latest.json', fakeFetch(GOOD, {calls}), () => clock);
  // Off until the user turns it on: a fresh install asks nobody at launch.
  assert.deepEqual(reminder.settings, {enabled: false});
  assert.deepEqual(await reminder.check(), {status: 'skipped'});
  assert.equal(calls.length, 0, 'off by default: nothing is fetched at launch');
  assert.deepEqual(readdirSync(directory), [], 'nothing is written before the user decides');
  assert.deepEqual(reminder.setEnabled(true), {enabled: true});
  const first = await reminder.check();
  assert.equal(first.status, 'available');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://rockycode.ai/desktop/latest.json');
  assert.deepEqual(JSON.parse(readFileSync(join(directory, 'update-reminder.json'), 'utf8')), {enabled: true, lastCheck: clock, skipped: null});
  // Within a day the launch check does not even fetch.
  clock += DAY - 1;
  assert.deepEqual(await reminder.check(), {status: 'skipped'});
  assert.equal(calls.length, 1);
  clock += 2;
  assert.equal((await reminder.check()).status, 'available');
  assert.equal(calls.length, 2);
  // "Later" hides this version until a newer one appears; a manual check still shows it.
  reminder.skip('0.2.0');
  clock += DAY + 1;
  assert.deepEqual(await reminder.check(), {status: 'skipped'});
  assert.equal(calls.length, 3, 'the feed is still fetched: a newer version than the skipped one must surface');
  assert.equal((await reminder.check(true)).status, 'available');
  // The reminder only ever opens the link the accepted feed named.
  assert.equal(reminder.link('https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0'), 'https://github.com/cicialgo/rockycode/releases/tag/desktop-v0.2.0');
  assert.equal(reminder.link('https://github.com/cicialgo/rockycode/releases/tag/other'), null);
  // State survives a restart.
  const reopened = new UpdateReminder(directory, '0.1.0', 'https://rockycode.ai/desktop/latest.json', fakeFetch(GOOD), () => clock);
  assert.deepEqual(await reopened.check(), {status: 'skipped'});
  assert.equal(reopened.link('https://rockycode.ai/'), null, 'a fresh process has accepted no feed yet');
});

test('up to date, the off switch and failures', async t => {
  const directory = temporaryDirectory(t);
  const calls: {url: string; init: unknown}[] = [];
  const current = new UpdateReminder(directory, '0.2.0', 'https://rockycode.ai/desktop/latest.json', fakeFetch(GOOD, {calls}), () => 5_000_000);
  current.setEnabled(true);
  assert.deepEqual(await current.check(), {status: 'upToDate', version: '0.2.0'});
  assert.deepEqual(current.setEnabled(false), {enabled: false});
  assert.deepEqual(await current.check(), {status: 'skipped'});
  assert.equal(calls.length, 1, 'disabled: nothing is fetched at launch');
  assert.equal((await current.check(true)).status, 'upToDate', 'a manual check still works while disabled');
  assert.equal(new UpdateReminder(directory, '0.2.0').settings.enabled, false, 'the switch persists');
  assert.throws(() => current.setEnabled('yes' as unknown as boolean), /Invalid update preference/);
  for (const [name, fetchFeed] of Object.entries({
    'network error': (async () => { throw new TypeError('fetch failed'); }) as FetchFeed,
    'http error': fakeFetch(GOOD, {ok: false}),
    'oversized by header': fakeFetch(GOOD, {length: FEED_LIMIT + 1}),
    'oversized body': fakeFetch('x'.repeat(FEED_LIMIT + 1)),
    'malformed': fakeFetch('{"version":"0.9.0","url":"http://rockycode.ai/"}'),
  })) {
    const reminder = new UpdateReminder(temporaryDirectory(t), '0.1.0', 'https://rockycode.ai/desktop/latest.json', fetchFeed, () => 1);
    reminder.setEnabled(true);
    assert.deepEqual(await reminder.check(true), {status: 'failed'}, name);
    assert.deepEqual(await reminder.check(), {status: 'failed'}, `${name}: a failure does not count as today's check`);
  }
});

test('a corrupt state file falls back to defaults and an unwritable directory fails loudly', async t => {
  const directory = temporaryDirectory(t);
  // Every unreadable field falls back to its default (the launch check: off); a readable one is kept.
  for (const [saved, enabled] of [['{', false], ['[]', false], ['null', false], ['{"enabled":"no","lastCheck":-1,"skipped":"latest"}', false], ['{"enabled":true,"lastCheck":"x"}', true]] as const) {
    writeFileSync(join(directory, 'update-reminder.json'), saved);
    const reminder = new UpdateReminder(directory, '0.1.0', 'https://rockycode.ai/desktop/latest.json', fakeFetch(GOOD), () => 1);
    assert.deepEqual(reminder.settings, {enabled}, saved);
    assert.equal((await reminder.check()).status, enabled ? 'available' : 'skipped', saved);
    assert.equal((await reminder.check(true)).status, 'available', saved);
  }
  const file = join(directory, 'file');
  writeFileSync(file, 'untouched');
  const blocked = new UpdateReminder(file, '0.1.0', 'https://rockycode.ai/desktop/latest.json', fakeFetch(GOOD), () => 1);
  assert.throws(() => blocked.setEnabled(true), {message: 'Could not save update preference'});
  assert.deepEqual(blocked.settings, {enabled: false}, 'a choice that could not be saved is not claimed');
  assert.equal((await blocked.check(true)).status, 'available', 'an unsaved lastCheck never hides a result');
  assert.equal((await blocked.check(true)).status, 'available');
  assert.equal(readFileSync(file, 'utf8'), 'untouched');
  mkdirSync(join(directory, 'nested'));
  assert.deepEqual(new UpdateReminder(join(directory, 'nested', 'new'), '0.1.0').setEnabled(false), {enabled: false});
});
