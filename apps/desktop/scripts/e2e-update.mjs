// The update reminder against a local feed: one plain GET at launch, a banner
// with the version, the link opened only through the main process, "Later"
// persisted, "Check now" ignoring the throttle, the off switch persisted.
// Offline fixture, no model calls, no real tools.
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

const root = resolve('.');
const data = await mkdtemp(join(tmpdir(), 'rockycode-update-e2e-'));
await writeFile(join(data, 'ui-preferences.json'), JSON.stringify({language: 'en'}));
const link = 'https://github.com/cicialgo/rockycode/releases/tag/desktop-v9.9.9';
const requests = [];
const server = createServer((request, response) => {
  requests.push({url: request.url, method: request.method, cookie: request.headers.cookie ?? null});
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({version: '9.9.9', url: link, notes: 'Test feed'}));
});
await new Promise(ready => server.listen(0, '127.0.0.1', ready));
const feed = `http://127.0.0.1:${server.address().port}/latest.json`;
const env = {...process.env, ROCKYCODE_DESKTOP_FIXTURE: '1', ROCKYCODE_DESKTOP_DEV: '1', ROCKYCODE_DESKTOP_TEST: '1', ROCKYCODE_DESKTOP_DATA_DIR: data, ROCKYCODE_DESKTOP_UPDATE_FEED: feed};
delete env.ELECTRON_RUN_AS_NODE;

async function launch() {
  const app = await electron.launch({executablePath: electronPath(), args: ['.'], cwd: root, env});
  const page = await app.firstWindow();
  await page.locator('.language-picker select').waitFor();
  await page.waitForFunction(() => !document.querySelector('.language-picker select')?.disabled);
  return {app, page};
}
const settle = page => page.waitForTimeout(700);

let {app, page} = await launch();
try {
  // A fresh install asks nobody: the launch check is off until the user turns it on.
  await settle(page);
  assert.equal(await page.getByLabel('Check for updates on launch').isChecked(), false);
  assert.equal(await page.getByTestId('update-banner').count(), 0);
  assert.equal(requests.length, 0, 'off by default: no request at launch');
  await page.getByLabel('Check for updates on launch').click();
  await page.waitForFunction(() => document.querySelector('.update-toggle input')?.checked);
  await app.close();
  ({app, page} = await launch());
  // Launch check: exactly one GET of the file, nothing else in the request.
  await page.getByTestId('update-banner').waitFor();
  await page.getByText('RockyCode 9.9.9 is available.').waitFor();
  assert.deepEqual(requests, [{url: '/latest.json', method: 'GET', cookie: null}]);
  // The button opens the link the feed named, through the main process only.
  await app.evaluate(({shell}) => { globalThis.opened = []; shell.openExternal = async url => { globalThis.opened.push(url); }; });
  await page.getByRole('button', {name: 'Open download page'}).click();
  await app.evaluate(() => new Promise(done => { const poll = () => globalThis.opened.length ? done() : setTimeout(poll, 20); poll(); }));
  assert.deepEqual(await app.evaluate(() => globalThis.opened), [link]);
  // "Later" closes the banner and skips this version; a restart within the day fetches nothing.
  await page.getByRole('button', {name: 'Later'}).click();
  await page.getByTestId('update-banner').waitFor({state: 'detached'});
  await app.close();
  ({app, page} = await launch());
  await settle(page);
  assert.equal(await page.getByTestId('update-banner').count(), 0, 'skipped version stays quiet');
  assert.equal(requests.length, 1, 'no fetch within a day');
  // "Check now" ignores the throttle and the skipped version.
  await page.getByRole('button', {name: 'Check now'}).click();
  await page.getByTestId('update-banner').waitFor();
  assert.equal(requests.length, 2);
  // The off switch is a visible, persisted preference.
  const toggle = page.getByLabel('Check for updates on launch');
  assert.equal(await toggle.isChecked(), true);
  await toggle.click();
  await page.waitForFunction(() => !document.querySelector('.update-toggle input')?.checked);
  await app.close();
  const state = JSON.parse(await readFile(join(data, 'update-reminder.json'), 'utf8'));
  assert.equal(state.enabled, false);
  assert.equal(state.skipped, '9.9.9');
  ({app, page} = await launch());
  await settle(page);
  assert.equal(await page.getByLabel('Check for updates on launch').isChecked(), false);
  assert.equal(await page.getByTestId('update-banner').count(), 0);
  assert.equal(requests.length, 2, 'disabled: no fetch at launch');
  // Chinese labels come from the same catalog.
  await page.getByRole('combobox', {name: 'Interface language'}).selectOption('zh');
  await page.getByLabel('启动时检查更新').waitFor();
  await page.screenshot({path: 'artifacts/update-reminder.png'});
  console.log('PASS: off by default (no request) + launch check once on (one plain GET) + banner + link via main process + later persisted + check now + off switch persisted + zh labels');
  console.log(`Fixture only: no paid model calls or actual agent tools. Feed: ${feed}`);
} finally {
  await app?.close();
  server.close();
}
