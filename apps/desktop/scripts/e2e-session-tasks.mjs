// Run after npm run build in the desktop directory. Offline Electron fixture.
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const data = await mkdtemp(join(tmpdir(), 'rocky-session-tasks-'));
await writeFile(join(data, 'ui-preferences.json'), JSON.stringify({language: 'zh'}));
const env = {...process.env, ROCKYCODE_DESKTOP_FIXTURE: '1', ROCKYCODE_DESKTOP_DEV: '1',
  ROCKYCODE_DESKTOP_TEST: '1', ROCKYCODE_DESKTOP_DATA_DIR: data, ROCKYCODE_DESKTOP_TEST_WORKSPACE: data};
delete env.ELECTRON_RUN_AS_NODE;
const launch = () => electron.launch({executablePath: electronPath(), args: ['.'], env});
let app = await launch();
try {
  let page = await app.firstWindow();
  await page.getByRole('button', {name: '选择工作目录'}).click();
  await page.getByLabel('任务输入').fill('保留来源草稿');
  const parent = (await page.evaluate(() => window.rocky.initialize())).threads[0].id;
  await page.waitForFunction(async id => (await window.rocky.initialize()).threads.find(t => t.id === id)?.draft === '保留来源草稿', parent);
  await page.getByTestId('task-connections').locator('summary').click();
  await page.getByLabel('新任务交接内容').fill('独立检查接口');
  await page.getByTestId('create-linked-task').click();
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="任务输入"]')?.value === '独立检查接口');
  let snapshot = await page.evaluate(() => window.rocky.initialize());
  const child = snapshot.threads.find(t => t.parentTaskId === parent);
  assert(child);
  assert.equal(child.messages.length, 0);
  assert.equal(child.permissionMode, 'ask');
  assert.equal(snapshot.threads.find(t => t.id === parent).draft, '保留来源草稿');
  await page.getByTestId('task-connections').locator('summary').click();
  await page.getByLabel('消息接收任务').selectOption(parent);
  await page.getByLabel('发送给另一任务的消息').fill('检查完成，请看结果');
  await page.getByRole('button', {name: '发送消息', exact: true}).click();
  await page.getByRole('status').filter({hasText: '已保存到对方收件箱'}).waitFor();
  await page.getByRole('button', {name: /^来源任务:/}).click();
  await page.getByTestId('task-connections').locator('summary').click();
  await page.getByRole('button', {name: '追加到输入草稿'}).click();
  assert.match(await page.getByLabel('任务输入').inputValue(), /保留来源草稿[\s\S]*检查完成/);
  await page.getByRole('button', {name: '回复发送方'}).click();
  await page.getByLabel('发送给另一任务的消息').fill('已收到，谢谢');
  await page.getByRole('button', {name: '发送消息', exact: true}).click();
  await page.getByRole('status').filter({hasText: '已保存到对方收件箱'}).waitFor();
  await app.close();
  app = await launch();
  page = await app.firstWindow();
  await page.getByLabel('任务输入').waitFor();
  snapshot = await page.evaluate(() => window.rocky.initialize());
  assert.equal(snapshot.threads.find(t => t.id === child.id).taskInbox[0].content, '已收到，谢谢');
  assert.equal(snapshot.threads.find(t => t.id === parent).taskInbox[0].content, '检查完成，请看结果');
  assert(snapshot.threads.every(t => t.messages.length === 0), 'mail must not silently start model turns');
  await page.setViewportSize({width: 900, height: 620});
  await page.locator('.language-picker select').selectOption('en');
  await page.getByTestId('task-connections').locator('summary').click();
  await page.getByRole('button', {name: 'Create new task', exact: true}).waitFor();
  console.log('SESSION TASKS E2E OK: child handoff, drafts, inbox reply, restart, bilingual 900×620');
} finally {
  await app.close();
}
