import {test} from 'node:test';
import assert from 'node:assert/strict';
import type {MenuItemConstructorOptions} from 'electron';
import {nativeMenuTemplate} from '../src/shared/menu';

function children(menu: MenuItemConstructorOptions[]) {
  return menu.flatMap(item => item.submenu as MenuItemConstructorOptions[]);
}

test('native menus have translated top-level and explicit role labels', () => {
  const english = nativeMenuTemplate('en', false);
  const chinese = nativeMenuTemplate('zh', false);
  assert.deepEqual(english.map(item => item.label), ['RockyCode', 'Edit', 'Window']);
  assert.deepEqual(chinese.map(item => item.label), ['RockyCode', '编辑', '窗口']);
  const englishRoles = children(english).filter(item => item.role);
  const chineseRoles = children(chinese).filter(item => item.role);
  assert.equal(englishRoles.length, 12);
  assert.deepEqual(englishRoles.map(item => item.role), chineseRoles.map(item => item.role));
  for (let index = 0; index < englishRoles.length; index++) {
    assert.ok(englishRoles[index].label);
    assert.ok(chineseRoles[index].label);
    assert.notEqual(englishRoles[index].label, chineseRoles[index].label, englishRoles[index].role ?? 'role');
  }
  assert.equal(englishRoles.find(item => item.role === 'copy')?.label, 'Copy');
  assert.equal(chineseRoles.find(item => item.role === 'copy')?.label, '拷贝');
});

test('packaged menus omit developer tools without changing role behavior', () => {
  for (const locale of ['en', 'zh'] as const) {
    const roles = children(nativeMenuTemplate(locale, true)).filter(item => item.role).map(item => item.role);
    assert.deepEqual(roles, ['about', 'hide', 'quit', 'undo', 'redo', 'cut', 'copy', 'paste', 'selectAll', 'minimize', 'zoom']);
  }
});

test('the update check is a menu item in both languages and calls the host action', () => {
  let calls = 0;
  for (const [locale, label] of [['en', 'Check for Updates…'], ['zh', '检查更新…']] as const) {
    const item = children(nativeMenuTemplate(locale, true, {checkForUpdates: () => { calls++; }})).find(entry => entry.label === label);
    assert.ok(item, label);
    assert.equal(item.role, undefined);
    (item.click as () => void)();
  }
  assert.equal(calls, 2);
  const silent = children(nativeMenuTemplate('en', true)).find(entry => entry.label === 'Check for Updates…');
  (silent?.click as () => void)();  // no action wired: a no-op, never a throw
});

test('switching menu language creates a fresh template', () => {
  const english = nativeMenuTemplate('en', false);
  const chinese = nativeMenuTemplate('zh', false);
  chinese[1].label = 'modified';
  assert.equal(english[1].label, 'Edit');
  assert.equal(nativeMenuTemplate('zh', false)[1].label, '编辑');
});
