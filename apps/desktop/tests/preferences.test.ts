import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {UiPreferences} from '../src/main/preferences';
import type {LanguagePreference} from '../src/shared/i18n';
import type {ThemePreference} from '../src/shared/theme';

function temporaryDirectory(t: {after: (cleanup: () => void) => void}) {
  const directory = mkdtempSync(join(tmpdir(), 'rocky-preferences-'));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  return directory;
}
const saved = (directory: string) => JSON.parse(readFileSync(join(directory, 'ui-preferences.json'), 'utf8'));

test('missing preferences follow the system and do not create files at startup', t => {
  const directory = temporaryDirectory(t);
  const preferences = new UiPreferences(directory, 'zh-CN');
  assert.deepEqual(preferences.language(), {preference: 'system', systemLocale: 'zh-CN'});
  assert.equal(preferences.theme(), 'system');
  assert.deepEqual(readdirSync(directory), []);
  const returned = preferences.language();
  returned.preference = 'en';
  assert.equal(preferences.language().preference, 'system');
});

test('language preference persists across restarts and can return to system', t => {
  const directory = join(temporaryDirectory(t), 'new-user-data');
  const preferences = new UiPreferences(directory, 'en-US');
  for (const preference of ['zh', 'en', 'system'] as const) {
    assert.deepEqual(preferences.setLanguage(preference), {preference, systemLocale: 'en-US'});
    assert.deepEqual(saved(directory), {language: preference, theme: 'system'});
    assert.deepEqual(new UiPreferences(directory, 'zh-TW').language(), {preference, systemLocale: 'zh-TW'});
    assert.deepEqual(readdirSync(directory), ['ui-preferences.json']);
  }
});

test('theme preference persists across restarts and can return to system', t => {
  const directory = join(temporaryDirectory(t), 'new-user-data');
  const preferences = new UiPreferences(directory, 'en-US');
  for (const theme of ['nebula', 'system', 'nebula'] as const) {
    assert.equal(preferences.setTheme(theme), theme);
    assert.deepEqual(saved(directory), {language: 'system', theme});
    assert.equal(new UiPreferences(directory, 'en-US').theme(), theme);
    assert.deepEqual(readdirSync(directory), ['ui-preferences.json']);
  }
});

test('saving one preference keeps the other', t => {
  const directory = temporaryDirectory(t);
  const preferences = new UiPreferences(directory, 'en-US');
  preferences.setLanguage('zh');
  preferences.setTheme('nebula');
  assert.deepEqual(saved(directory), {language: 'zh', theme: 'nebula'});
  preferences.setLanguage('en');
  assert.deepEqual(saved(directory), {language: 'en', theme: 'nebula'});
  const restarted = new UiPreferences(directory, 'en-US');
  assert.equal(restarted.language().preference, 'en');
  assert.equal(restarted.theme(), 'nebula');
});

test('a file written before themes existed still loads', t => {
  const directory = temporaryDirectory(t);
  writeFileSync(join(directory, 'ui-preferences.json'), '{"language":"zh"}\n');
  const preferences = new UiPreferences(directory, 'en-US');
  assert.equal(preferences.language().preference, 'zh');
  assert.equal(preferences.theme(), 'system');
});

test('malformed and unsupported saved preferences safely fall back to system', t => {
  const directory = temporaryDirectory(t);
  for (const content of ['{', '', 'null', '[]', 'true', '"zh"', '{}', '{"language":null}', '{"language":"fr"}', '{"language":"ZH"}', '{"language":{"value":"en"}}',
    '{"theme":null}', '{"theme":"dark"}', '{"theme":"NEBULA"}', '{"theme":["nebula"]}']) {
    writeFileSync(join(directory, 'ui-preferences.json'), content);
    const preferences = new UiPreferences(directory, 'fr-FR');
    assert.deepEqual(preferences.language(), {preference: 'system', systemLocale: 'fr-FR'}, content);
    assert.equal(preferences.theme(), 'system', content);
  }
});

test('one bad field does not discard the other', t => {
  const directory = temporaryDirectory(t);
  writeFileSync(join(directory, 'ui-preferences.json'), '{"language":"fr","theme":"nebula"}');
  assert.equal(new UiPreferences(directory, 'en-US').theme(), 'nebula');
  writeFileSync(join(directory, 'ui-preferences.json'), '{"language":"zh","theme":"sepia"}');
  assert.equal(new UiPreferences(directory, 'en-US').language().preference, 'zh');
});

test('an unreadable preference file does not block startup', t => {
  const directory = temporaryDirectory(t);
  mkdirSync(join(directory, 'ui-preferences.json'));
  const preferences = new UiPreferences(directory, 'en-GB');
  assert.deepEqual(preferences.language(), {preference: 'system', systemLocale: 'en-GB'});
  assert.equal(preferences.theme(), 'system');
});

test('invalid values cannot overwrite a saved preference', t => {
  const directory = temporaryDirectory(t);
  const preferences = new UiPreferences(directory, 'en-US');
  preferences.setLanguage('zh');
  preferences.setTheme('nebula');
  for (const invalid of ['fr', '', null, undefined, 1, {}, ['en']]) {
    assert.throws(() => preferences.setLanguage(invalid as LanguagePreference), /Invalid language preference/);
    assert.throws(() => preferences.setTheme(invalid as ThemePreference), /Invalid theme preference/);
    assert.equal(preferences.language().preference, 'zh');
    assert.equal(preferences.theme(), 'nebula');
    const restarted = new UiPreferences(directory, 'en-US');
    assert.equal(restarted.language().preference, 'zh');
    assert.equal(restarted.theme(), 'nebula');
  }
});

test('a failed write keeps the active preference and removes temporary files', t => {
  const directory = temporaryDirectory(t);
  const preferences = new UiPreferences(directory, 'en-US');
  preferences.setLanguage('zh');
  rmSync(join(directory, 'ui-preferences.json'));
  mkdirSync(join(directory, 'ui-preferences.json'));
  assert.throws(() => preferences.setLanguage('en'), {message: 'Could not save language preference'});
  assert.throws(() => preferences.setTheme('nebula'), {message: 'Could not save theme preference'});
  assert.equal(preferences.language().preference, 'zh');
  assert.equal(preferences.theme(), 'system');
  assert.deepEqual(readdirSync(directory), ['ui-preferences.json']);
});

test('a user-data directory that cannot be created fails safely', t => {
  const directory = temporaryDirectory(t);
  const file = join(directory, 'file');
  writeFileSync(file, 'untouched');
  const preferences = new UiPreferences(file, 'en-US');
  assert.throws(() => preferences.setLanguage('zh'), {message: 'Could not save language preference'});
  assert.throws(() => preferences.setTheme('nebula'), {message: 'Could not save theme preference'});
  assert.equal(preferences.language().preference, 'system');
  assert.equal(preferences.theme(), 'system');
  assert.equal(readFileSync(file, 'utf8'), 'untouched');
});
