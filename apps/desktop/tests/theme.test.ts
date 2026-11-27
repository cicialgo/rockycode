import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {applyTheme, colorScheme, isThemePreference, themeArgument, themeFromArguments, themes, windowBackground} from '../src/shared/theme';
import {catalogs} from '../src/shared/catalog';

test('only listed themes are accepted', () => {
  for (const theme of themes) assert.equal(isThemePreference(theme), true);
  for (const invalid of ['', 'dark', 'light', 'NEBULA', ' nebula', null, undefined, 1, {}, ['nebula']]) assert.equal(isThemePreference(invalid), false);
});

test('a dark theme pins the colour scheme and the window colour; System leaves both to the OS', () => {
  assert.equal(colorScheme('system'), 'system');
  assert.equal(windowBackground('system'), null);
  assert.equal(colorScheme('nebula'), 'dark');
  assert.equal(windowBackground('nebula'), '#16161e');
});

test('the saved theme survives the trip through the renderer arguments', () => {
  for (const theme of themes) assert.equal(themeFromArguments(['electron', '--some-flag', themeArgument(theme)]), theme);
  assert.equal(themeFromArguments(['electron']), 'system');
  assert.equal(themeFromArguments(['--rocky-theme=sepia']), 'system');
  assert.equal(themeFromArguments(['--rocky-theme=']), 'system');
});

test('System carries no attribute, so the base stylesheet is the system theme', () => {
  const root = {dataset: {} as Record<string, string>} as unknown as HTMLElement;
  applyTheme(root, 'nebula');
  assert.equal(root.dataset.theme, 'nebula');
  applyTheme(root, 'system');
  assert.equal('theme' in root.dataset, false);
});

test('every theme has a name in every language', () => {
  for (const [locale, catalog] of Object.entries(catalogs)) {
    for (const theme of themes) assert.ok((catalog as Record<string, string>)[`theme.${theme}`], `${locale}: theme.${theme}`);
    assert.ok((catalog as Record<string, string>)['theme.label'], `${locale}: theme.label`);
  }
});

// The Nebula window colour is stated twice (main process, stylesheet); they must agree.
test('the window colour behind the first paint is the stylesheet background', () => {
  const css = readFileSync(new URL('../src/renderer/nebula.css', import.meta.url), 'utf8');
  assert.match(css, new RegExp(`--n-bg:${windowBackground('nebula')}\\b`));
});
