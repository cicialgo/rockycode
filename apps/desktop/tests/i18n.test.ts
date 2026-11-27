import {test} from 'node:test';
import assert from 'node:assert/strict';
import {catalogs, en, zh, isLanguagePreference, locales, resolveLocale, translate, type Locale, type TranslationKey} from '../src/shared/i18n';
import {localizeCapabilityDetail, localizeKeyed} from '../src/shared/diagnostics';

const placeholders = (value: string) => [...new Set([...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]))].sort();
test('English and Chinese catalogs have identical keys and placeholders', () => {
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort());
  for (const key of Object.keys(en) as TranslationKey[]) {
    assert.ok(en[key].trim(), `empty English: ${key}`);
    assert.ok(zh[key].trim(), `empty Chinese: ${key}`);
    assert.deepEqual(placeholders(zh[key]), placeholders(en[key]), key);
  }
});
test('every exported locale names itself and has a document language tag', () => {
  assert.deepEqual(locales, ['en', 'zh']);
  for (const code of locales) {
    assert.ok(translate(code, 'language.name').trim(), code);
    assert.match(translate(code, 'language.tag'), /^[a-z]{2}(-[A-Z]{2})?$/, code);
  }
});
test('explicit preferences override the system; unsupported system locales fall back to English', () => {
  for (const locale of ['zh', 'zh-CN', 'zh-Hant-TW', 'ZH_hans']) assert.equal(resolveLocale('system', locale), 'zh');
  for (const locale of ['en-US', 'fr-FR', 'ja', '', 'zhang']) assert.equal(resolveLocale('system', locale), 'en');
  assert.equal(resolveLocale('en', 'zh-CN'), 'en');
  assert.equal(resolveLocale('zh', 'en-US'), 'zh');
  assert.ok(isLanguagePreference('system'));
  for (const value of ['', 'fr', null, undefined, {}, 42]) assert.equal(isLanguagePreference(value), false);
});
test('interpolation preserves values without recursively translating or evaluating them', () => {
  assert.equal(translate('en', 'runtime.tools', {count: 3}), 'Registered tools (3)');
  assert.equal(translate('zh', 'note.characters', {count: 12}), '12 字符');
  assert.equal(translate('en', 'runtime.tools', {count: '{count}<script>中文'}), 'Registered tools ({count}<script>中文)');
});
test('missing runtime catalog entries and unsupported locales fall back to English', () => {
  const mutable = catalogs.zh as Partial<Record<TranslationKey, string>>;
  const saved = mutable['newTask'];
  try { delete mutable['newTask']; assert.equal(translate('zh', 'newTask'), en.newTask); }
  finally { mutable['newTask'] = saved; }
  assert.equal(translate('fr' as Locale, 'newTask'), en.newTask);
  assert.equal(translate('en', 'future.key' as 'newTask'), 'future.key');
});
test('runtime messages localize by key only; text is never matched', () => {
  assert.equal(localizeKeyed('en', '会话不存在', 'diagnostic.sessionMissing'), 'Session not found');
  assert.equal(localizeKeyed('zh', 'Session not found', 'diagnostic.sessionMissing'), '会话不存在');
  assert.equal(localizeKeyed('zh', '{count} more files omitted', 'race.moreFilesOmitted', {count: 3}), '预览还省略了 3 个文件。');
  for (const text of ['Provider 429: retry in 6s', '会话不存在', 'constructor', 'toString', 'file /tmp/中文.md']) {
    assert.equal(localizeKeyed('en', text), text);
    assert.equal(localizeKeyed('zh', text, null), text);
    assert.equal(localizeKeyed('zh', text, 'diagnostic.notAKey'), text);
    assert.equal(localizeKeyed('zh', text, text), text, 'a sentence is not a key');
  }
  // a limitation is a key; an old host sending a sentence shows the sentence
  assert.equal(localizeKeyed('zh', 'diagnostic.limitationFixture', 'diagnostic.limitationFixture'), '不会调用模型或执行真实工具');
  assert.equal(localizeCapabilityDetail('zh', {detail: '3 skill(s) available', detail_key: 'capability_detail.skills', params: {count: 3}}), '3 个技能可用');
  assert.equal(localizeCapabilityDetail('en', {detail: 'x', detail_key: 'capability_detail.mcp_running', params: {servers: 2, tools: 7, failed: 1, untrusted: 3}}), '2 server(s), 7 tool(s); 1 failed or denied, 3 project config(s) untrusted');
  assert.equal(localizeCapabilityDetail('zh', {detail: 'prefix 3 skill(s) available', detail_key: null}), 'prefix 3 skill(s) available');
  assert.equal(localizeCapabilityDetail('zh', {detail: 'Provider 429', detail_key: 'not.a.key'}), 'Provider 429');
});
// These assertions are checked by tsc, never executed. Keep key and placeholder
// names type-safe without adding a runtime dependency.
function compileTimeContract() {
  // @ts-expect-error unknown translation key
  translate('en', 'missing.key');
  // @ts-expect-error required placeholder missing
  translate('en', 'runtime.tools');
  // @ts-expect-error misspelled placeholder
  translate('zh', 'runtime.tools', {total: 1});
  // @ts-expect-error no placeholders accepted for this key
  translate('en', 'newTask', {count: 1});
}
void compileTimeContract;
