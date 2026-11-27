import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile, writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
const [phase, directory] = process.argv.slice(2);
if (!phase) {
  const data = await mkdtemp(join(tmpdir(), 'rocky-task-ux-'));
  await mkdir('artifacts', {recursive: true});
  await writeFile(join(data, 'ui-preferences.json'), JSON.stringify({language: 'zh'}));
  // Separate driver processes model an actual app restart. Reusing Playwright's
  // Electron driver across launches can misroute mouse input to sandboxed frames.
  for (const step of ['settings', 'preview', 'reopen']) {
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url), step, data], {env: process.env, stdio: 'inherit'});
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
  }
  console.log('PASS: task UX end-to-end; fixture only, no API calls or external browser launch.');
  process.exit(0);
}
const data = directory;
const env = {...process.env, ROCKYCODE_DESKTOP_FIXTURE: '1', ROCKYCODE_DESKTOP_DEV: '1', ROCKYCODE_DESKTOP_TEST: '1', ROCKYCODE_DESKTOP_DATA_DIR: data, ROCKYCODE_DESKTOP_TEST_WORKSPACE: data};
delete env.ELECTRON_RUN_AS_NODE;
let app, page;
async function launch() {
  app = await electron.launch({executablePath: electronPath(), args: ['.'], cwd: resolve('.'), env});
  page = await app.firstWindow();
  await page.waitForFunction(() => !document.querySelector('.new-task')?.disabled);
  // Verify the explicit browser route without opening the user's browser.
  await app.evaluate(({shell}) => { globalThis.externalOpens = []; shell.openExternal = async url => { globalThis.externalOpens.push(url); }; });
}
async function snapshot() { return page.evaluate(() => window.rocky.initialize()); }
async function send(prompt) {
  await page.getByLabel('任务输入', {exact: true}).fill(prompt);
  await page.getByRole('button', {name: '发送任务', exact: true}).click();
}
async function idle() { await page.getByRole('button', {name: '停止任务', exact: true}).waitFor({state: 'hidden'}); }
try {
  await launch();
  if (phase === 'settings') {
    await page.getByRole('button', {name: '选择工作目录', exact: true}).click();
    await page.getByLabel('任务模型', {exact: true}).selectOption('fixture:reasoning');
    const first = (await snapshot()).threads[0].id;
    await send('Task A [long]');
    await page.getByRole('button', {name: '停止任务', exact: true}).waitFor();
    await page.waitForFunction(() => document.querySelector('[aria-label=任务模型]')?.disabled);
    await page.locator('.new-task').click();
    await page.getByLabel('任务模型', {exact: true}).selectOption('fixture:fast');
    const second = (await snapshot()).threads.find(t => t.id !== first).id;
    let tasks = (await snapshot()).threads;
    assert.equal(tasks.find(t => t.id === first).model, 'fixture:reasoning');
    assert.equal(tasks.find(t => t.id === second).model, 'fixture:fast');
    await page.locator('.thread-button').filter({hasText: 'Task A'}).click();
    await page.getByRole('button', {name: '停止任务', exact: true}).click();
    await idle();
    await app.close();
    await launch();
    assert.equal(await page.getByLabel('任务模型', {exact: true}).inputValue(), 'fixture:reasoning');
    await page.locator('.new-task').click();
    await page.getByLabel('任务模型', {exact: true}).selectOption('fixture:fast');
    await page.getByRole('button', {name: '审批模式说明', exact: true}).click();
    await page.getByText(/Codex 将沙箱范围与审批策略分开/).waitFor();
    await page.getByRole('button', {name: '审批模式说明', exact: true}).click();
    await page.getByLabel('审批模式', {exact: true}).selectOption('yolo');
    await send('[mcp]');
    await page.getByText('连接工具服务“example-docs”？', {exact: true}).waitFor();
    await page.getByText('~/.codex/config.toml', {exact: true}).waitFor();
    await page.getByText(/自动从你本机的 Codex 配置读取/).waitFor();
    await page.screenshot({path: 'artifacts/task-mcp-provenance.png'});
    await page.getByRole('button', {name: '本次应用运行期间记住此连接', exact: true}).click();
    await idle();
    await page.locator('.new-task').click();
    await send('[mcp]');
    await page.getByText('Connected simulated service.', {exact: true}).waitFor();
    assert.equal(await page.locator('.connection-approval').count(), 0);
    await idle();
    await writeFile(join(data, 'fixture-task-ids.json'), JSON.stringify({first, second}));
    console.log('PASS: independent task models, active-task guard, persistence, approval explanation, MCP provenance and exact remembered consent.');
  } else if (phase === 'preview') {
    await page.getByLabel('任务输入', {exact: true}).waitFor();
    await send('[artifact]');
    const preview = page.frameLocator('iframe[title="HTML 预览"]');
    await preview.getByRole('heading', {name: 'Project preview', exact: true}).waitFor();
    await idle();
    assert.deepEqual(await app.evaluate(() => globalThis.externalOpens), []);
    const frame = page.frames().find(frame => frame.url().startsWith('rocky-artifact:'));
    assert.equal(await page.locator('iframe[title="HTML 预览"]').getAttribute('sandbox'), '');
    assert.equal(await frame.evaluate(() => document.body.dataset.ipc), undefined, 'fixture inline script must not execute');
    await preview.getByRole('button', {name: 'Try it', exact: true}).click();
    await page.waitForTimeout(250);
    await preview.getByRole('button', {name: 'Try it', exact: true}).waitFor();
    assert.equal(await preview.getByRole('button', {name: 'Interactive works', exact: true}).count(), 0);
    assert.equal(await frame.evaluate(() => typeof window.rocky), 'undefined');
    assert.equal(await frame.evaluate(() => { try { return typeof parent.rocky; } catch { return 'blocked'; } }), 'blocked');
    assert.equal(await frame.evaluate(async () => { try { await fetch('https://example.invalid/'); return 'allowed'; } catch { return 'blocked'; } }), 'blocked');
    await page.getByRole('button', {name: '在默认浏览器中打开', exact: true}).click();
    await page.waitForTimeout(100);
    const external = await app.evaluate(() => globalThis.externalOpens);
    assert.equal(external.length, 1);
    assert.match(external[0], /^file:.*fixture-preview\.html$/);
    const current = (await snapshot()).threads[0].id;
    assert.equal(await page.evaluate(async id => { try { await window.rocky.previewArtifact(id, 'https://example.invalid/a.html'); return false; } catch { return true; } }, current), true);
    await page.getByRole('combobox', {name: '界面语言', exact: true}).selectOption('en');
    await page.getByRole('button', {name: 'Open in default browser', exact: true}).waitFor();
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].setSize(900, 620));
    await page.waitForFunction(() => innerWidth <= 900);
    const outside = await page.locator('.composer-box,.artifact-panel,.artifact-panel header button').evaluateAll(elements => elements.filter(el => { const b = el.getBoundingClientRect(); return b.left < 0 || b.right > innerWidth + 1 || b.top < 0 || b.bottom > innerHeight + 1; }).map(el => el.className));
    assert.deepEqual(outside, []);
    await page.screenshot({path: 'artifacts/task-html-preview.png'});
    await page.getByRole('button', {name: 'Close preview', exact: true}).click();
    await page.locator('.prose .artifact-link').click();
    await page.frameLocator('iframe[title="HTML preview"]').getByRole('heading', {name: 'Project preview', exact: true}).waitFor();
    const original = `<!doctype html><meta http-equiv="Content-Security-Policy" content="script-src 'unsafe-inline'"><style>h1{color:rgb(17, 34, 51)}</style><h1>Updated preview</h1><button onclick="this.textContent='Restored interaction works'">Restored try</button><script>document.body.dataset.executed="yes"</script>`;
    await writeFile(join(data, 'fixture-preview.html'), original);
    await page.getByRole('button', {name: 'Reload preview', exact: true}).click();
    await page.frameLocator('iframe[title="HTML preview"]').getByRole('heading', {name: 'Updated preview', exact: true}).waitFor();
    const refreshed = page.frames().find(frame => frame.url().startsWith('rocky-artifact:'));
    assert.equal(await refreshed.evaluate(() => document.body.dataset.executed), undefined, 'document meta CSP cannot relax the response policy');
    assert.equal(await refreshed.locator('h1').evaluate(el => getComputedStyle(el).color), 'rgb(17, 34, 51)');
    assert.equal(await readFile(join(data, 'fixture-preview.html'), 'utf8'), original, 'preview must preserve the interactive original');
    console.log('PASS: static HTML, blocked inline scripts/events, parent/IPC/network isolation, explicit browser arrow, Markdown link, refresh and bilingual narrow layout.');
  } else if (phase === 'reopen') {
    const preview = page.frameLocator('iframe[title="HTML preview"]');
    await preview.getByRole('heading', {name: 'Updated preview', exact: true}).waitFor();
    await preview.getByRole('button', {name: 'Restored try', exact: true}).click();
    await page.waitForTimeout(250);
    await preview.getByRole('button', {name: 'Restored try', exact: true}).waitFor();
    assert.equal(await preview.getByRole('button', {name: 'Restored interaction works', exact: true}).count(), 0);
    const frame = page.frames().find(frame => frame.url().startsWith('rocky-artifact:'));
    assert.equal(await frame.evaluate(() => document.body.dataset.executed), undefined);
    const {first, second} = JSON.parse(await readFile(join(data, 'fixture-task-ids.json'), 'utf8'));
    const tasks = (await snapshot()).threads;
    assert.equal(tasks.find(t => t.id === first).model, 'fixture:reasoning');
    assert.equal(tasks.find(t => t.id === second).model, 'fixture:fast');
    console.log('PASS: a new app process restores models and static HTML without restoring script execution.');
  } else throw new Error('Unknown test phase');
} catch (error) {
  await page?.screenshot({path: 'artifacts/task-ux-failure.png'}).catch(() => {});
  console.error('Visible error:', await page?.locator('[role=alert]').allTextContents());
  throw error;
} finally { await app?.close().catch(() => {}); }
