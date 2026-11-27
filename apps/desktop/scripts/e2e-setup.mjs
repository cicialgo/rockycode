// Add a provider key from inside the app, on a first launch with nothing configured.
// Live runtime against a throwaway home and a local stand-in for the provider: the key
// test (GET /models) lands on 127.0.0.1, so no real key, no model call, no network.
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp, readFile, stat, writeFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

const GOOD = 'e2e-accepted-key';
const BAD = 'e2e-rejected-key';
const requests = [];
const server = createServer((request, response) => {
  const accepted = request.headers.authorization === `Bearer ${GOOD}`;
  requests.push({url: request.url, method: request.method, accepted});
  response.setHeader('Content-Type', 'application/json');
  response.statusCode = accepted ? 200 : 401;
  response.end(JSON.stringify(accepted ? {object: 'list', data: []} : {error: {message: 'bad key'}}));
});
await new Promise(ready => server.listen(0, '127.0.0.1', ready));

const data = await mkdtemp(join(tmpdir(), 'rockycode-setup-e2e-'));
const home = await mkdtemp(join(tmpdir(), 'rockycode-setup-home-'));
await writeFile(join(data, 'ui-preferences.json'), JSON.stringify({language: 'en'}));
const env = {...process.env, ROCKYCODE_DESKTOP_DEV: '1', ROCKYCODE_DESKTOP_DATA_DIR: data, ROCKYCODE_HOME: home,
  ROCKYCODE_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, ROCKYCODE_OLLAMA_URL: 'http://127.0.0.1:9'};
delete env.ELECTRON_RUN_AS_NODE;
for (const name of Object.keys(env)) if (/^ROCKYCODE_(.*_)?API_KEY$|^ROCKYCODE_MODEL$/.test(name)) delete env[name];

const app = await electron.launch({executablePath: electronPath(), args: ['.'], cwd: resolve('.'), env});
try {
  const page = await app.firstWindow();
  await page.locator('.language-picker select').waitFor();
  // Nothing can run: the start page offers the key form, not only a terminal command.
  const card = page.locator('.setup-card');
  await card.waitFor({timeout: 20000});
  const form = card.locator('.key-form');
  await form.waitFor();
  const field = form.locator('input');
  assert.equal(await field.getAttribute('type'), 'password', 'the key is typed hidden');
  const options = await form.locator('select option').allTextContents();
  assert.ok(options.some(text => text.startsWith('DeepSeek')) && options.some(text => text.includes('Kimi')), options.join(' | '));
  assert.ok(!options.some(text => /ollama/i.test(text)), 'a local server takes no key');
  // A throwaway home never gets the system keychain: this test cannot reach the real one.
  assert.equal(await form.locator('input[type=checkbox]').count(), 0, 'no keychain choice on a relocated store');
  await form.locator('select').selectOption('deepseek');
  await page.screenshot({path: 'artifacts/setup-card.png'});

  // A rejected key: said in place, nothing saved, the field emptied.
  await field.fill(BAD);
  await form.getByRole('button', {name: 'Save key'}).click();
  await form.getByText('That key was rejected (401).', {exact: false}).waitFor();
  assert.equal(await field.inputValue(), '');
  assert.equal(existsSync(join(home, '.env')), false, 'a rejected key is never written');
  assert.equal(await card.count(), 1);

  // The right key, sent with Enter: tested, saved, and the app is ready without a relaunch.
  await field.fill(GOOD);
  await field.press('Enter');
  await card.waitFor({state: 'detached', timeout: 30000});
  await page.locator('.app-footer').getByText('deepseek:deepseek-flash').waitFor();
  const saved = await readFile(join(home, '.env'), 'utf8');
  assert.deepEqual(saved.trim().split('\n'), [`ROCKYCODE_API_KEY=${GOOD}`]);
  assert.equal((await stat(join(home, '.env'))).mode & 0o777, 0o600, 'the key file is private');
  assert.deepEqual(requests, [{url: '/v1/models', method: 'GET', accepted: false}, {url: '/v1/models', method: 'GET', accepted: true}]);
  const everywhere = await page.evaluate(() => document.documentElement.outerHTML + JSON.stringify(localStorage));
  assert.ok(!everywhere.includes(GOOD) && !everywhere.includes(BAD), 'the key is nowhere in the page');
  for (const file of ['ui-preferences.json']) assert.ok(!(await readFile(join(data, file), 'utf8')).includes(GOOD));

  // Later keys go through the runtime dialog: the same form, any time.
  await page.locator('.runtime-pill').click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('heading', {name: 'Add or replace a provider key'}).waitFor();
  await dialog.locator('.key-form select option').first().waitFor({state: 'attached'});
  const again = await dialog.locator('.key-form select option').allTextContents();
  assert.ok(again.some(text => text.startsWith('DeepSeek') && text.includes('key in the key file')), again.join(' | '));
  await page.screenshot({path: 'artifacts/setup-key.png'});

  // The keychain choice. The main process answers for the host from here on, so the real
  // system keychain is never involved: where there is one the box is ticked, the tick is
  // what is sent, and where the key went is said. A keychain that does not take the key
  // saves nothing: the form says so, and the key file is the user's own untick.
  await app.evaluate(({ipcMain}) => {
    const sent = (globalThis.__keySaves = []);
    ipcMain.removeHandler('desktop:key-providers');
    ipcMain.handle('desktop:key-providers', () => ({keychain: true, providers: [
      {id: 'deepseek', label: 'DeepSeek', host: 'api.deepseek.com', configured: true, place: 'keychain'},
      {id: 'kimi', label: 'Kimi / Moonshot', host: 'api.moonshot.cn', configured: false, place: ''}]}));
    ipcMain.removeHandler('desktop:save-key');
    ipcMain.handle('desktop:save-key', (_event, endpoint, key, keychain) => {
      sent.push({endpoint, keychain, length: String(key).length});
      if (keychain && endpoint === 'kimi') throw new Error('The system keychain did not take the key, so nothing was saved. Unlock it and save again, or untick the keychain box to keep the key in the key file.');
      return {runtime: null, verified: true, place: keychain ? 'keychain' : 'file'};
    });
  });
  await dialog.locator('.modal-close').click();
  await page.locator('.runtime-pill').click();
  const chained = page.getByRole('dialog').locator('.key-form');
  const tick = chained.locator('input[type=checkbox]');
  await tick.waitFor();
  assert.equal(await tick.isChecked(), true, 'the system keychain comes first');
  assert.ok((await chained.locator('select option').allTextContents()).includes('DeepSeek · key in the system keychain'));
  const secret = chained.locator('input[type=password]');
  await chained.locator('select').selectOption('deepseek');
  await secret.fill('stand-in-key-1');
  await chained.getByRole('button', {name: 'Save key'}).click();
  await chained.getByText('Key saved in the system keychain.', {exact: true}).waitFor();
  await tick.uncheck();
  await secret.fill('stand-in-key-2');
  await secret.press('Enter');
  await chained.getByText('Key saved.', {exact: true}).waitFor();
  await tick.check();
  await chained.locator('select').selectOption('kimi');
  await secret.fill('stand-in-key-3');
  await secret.press('Enter');
  await chained.getByText('The system keychain did not take the key, so nothing was saved. Unlock it and save again, or untick the keychain box to keep the key in the key file.', {exact: true}).waitFor();
  await tick.uncheck();  // the plain-text file, chosen by hand
  await secret.fill('stand-in-key-3');
  await secret.press('Enter');
  await chained.getByText('Key saved.', {exact: true}).waitFor();
  assert.deepEqual(await app.evaluate(() => globalThis.__keySaves), [
    {endpoint: 'deepseek', keychain: true, length: 14}, {endpoint: 'deepseek', keychain: false, length: 14},
    {endpoint: 'kimi', keychain: true, length: 14}, {endpoint: 'kimi', keychain: false, length: 14}]);
  await page.screenshot({path: 'artifacts/setup-keychain.png'});
  console.log('PASS: first launch key form — rejected key said in place, accepted key saved 0600 and ready without relaunch, key nowhere in the page, form in the runtime dialog');
  console.log('PASS: keychain choice — ticked where there is one, the tick is what is sent, a refusal saves nothing and says so (main process stands in: no real keychain)');
  console.log('Local stand-in provider only: no real key, no model call.');
} finally {
  await app.close();
  server.close();
}
