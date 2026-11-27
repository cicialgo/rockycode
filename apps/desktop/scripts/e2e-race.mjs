// Offline Electron / preload / IPC / Python fixture coverage. Never calls paid models.
// Run after npm run build: npm run test:e2e:race
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root = fileURLToPath(new URL('../', import.meta.url));
const data = await mkdtemp(join(tmpdir(), 'rockycode-race-e2e-'));
const workspace = join(data, 'workspace');
const artifacts = join(root, 'artifacts');
await mkdir(workspace, {recursive: true});
await mkdir(artifacts, {recursive: true});
const state = join(data, 'state');
await mkdir(state, {recursive: true});
// Chinese assertions below hold regardless of the machine locale, as in e2e.mjs.
await writeFile(join(state, 'ui-preferences.json'), JSON.stringify({language: 'zh'}));
await writeFile(join(workspace, 'input.txt'), 'Synthetic offline model-race input.\n');
const env = {
  ...process.env,
  ROCKYCODE_DESKTOP_FIXTURE: '1',
  ROCKYCODE_DESKTOP_DEV: '1',
  ROCKYCODE_DESKTOP_TEST: '1',
  ROCKYCODE_DESKTOP_DATA_DIR: state,
  ROCKYCODE_DESKTOP_TEST_WORKSPACE: workspace,
};
delete env.ELECTRON_RUN_AS_NODE;
const models = Array.from({length: 5}, (_, index) => `fixture:model-${index + 1}`);
const aliases = ['A', 'B', 'C', 'D', 'E'];
const limits = {maxSteps: 3, maxTokens: 512, timeoutSeconds: 30};
const report = {mode: 'offline-fixture-only', result: 'running', checks: []};
let app;
let page;
let currentCheck = 'launch';

async function check(name, action) {
  currentCheck = name;
  await action();
  report.checks.push(name);
  console.log(`PASS: ${name}`);
}

async function launch() {
  app = await electron.launch({executablePath: electronPath(), args: ['.'], cwd: root, env});
  page = await app.firstWindow();
  page.setDefaultTimeout(15_000);
  await page.getByTestId('race-nav').waitFor();
  await page.waitForFunction(() => !document.querySelector('.language-picker select')?.disabled);
  const registry = await page.evaluate(() => window.rocky.listRaces());
  assert.equal(registry.fixture, true, 'refuse to run against a live model registry');
  for (const model of models) assert.ok(registry.models.some(entry => entry.id === model), `missing fixture ${model}`);
  assert.ok(registry.models.every(model => model.id.startsWith('fixture:')), 'fixture registry must not expose live models');
}

async function readRace(id) {
  const race = await page.evaluate(raceId => window.rocky.getRace(raceId), id);
  assert.equal(race.detailsLoaded, true, 'detail reads must return the full persisted race');
  return race;
}

// Backend state is polled from Node: Playwright's waitForFunction does not await an
// async predicate, so a Promise-returning one resolves immediately on its truthiness.
async function waitForRaceState(id, test, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const race = await page.evaluate(raceId => window.rocky.getRace(raceId), id);
    if (race.detailsLoaded && test(race)) return race;
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${label}: ` + JSON.stringify({status: race.status, judge: race.judge.status, lanes: race.lanes.map(lane => [lane.alias, lane.status, lane.content.length])}));
    await delay(100);
  }
}

function waitForRace(id, status) {
  return waitForRaceState(id, race => race.status === status, `race ${status}`);
}

async function waitForLane(alias, status) {
  await page.waitForFunction(({alias, status}) =>
    document.querySelector(`[data-testid="race-lane-${alias}"]`)?.getAttribute('data-status') === status,
  {alias, status});
}

async function startFromUI(prompt) {
  await page.getByTestId('race-prompt').fill(prompt);
  for (const [index, model] of models.entries()) await page.getByTestId(`race-model-${index}`).selectOption(model);
  const previous = await page.evaluate(async () => (await window.rocky.listRaces()).races.map(race => race.id));
  await page.getByTestId('race-start').click();
  const deadline = Date.now() + 15_000;
  let id = null;
  while (!id) {
    const fresh = await page.evaluate(async () => (await window.rocky.listRaces()).races.map(race => race.id));
    id = fresh.find(raceId => !previous.includes(raceId)) ?? null;
    if (!id && Date.now() > deadline) assert.fail('the started race never appeared in history');
    if (!id) await delay(50);
  }
  const race = await readRace(id);
  assert.equal(race.prompt, prompt);
  assert.equal(race.fixture, true);
  assert.equal(race.lanes.length, 5);
  assert.deepEqual([...new Set(race.lanes.map(lane => lane.model))].sort(), [...models].sort());
  assert.deepEqual(race.lanes.map(lane => lane.alias).sort(), aliases);
  return race;
}

async function waitForDialogCalls(count) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (await app.evaluate((_electron, expected) => globalThis.__raceE2EDialogCalls === expected, count)) return;
    await delay(50);
  }
  assert.fail(`expected ${count} fixture export save dialog call(s)`);
}

try {
  await launch();
  await check('language switching and navigation retain separate drafts', async () => {
    await page.getByRole('button', {name: '选择工作目录', exact: true}).click();
    const chatDraft = 'A chat draft kept while comparing models.';
    const raceDraft = '比较五个模型。Keep this race draft across navigation.';
    await page.getByLabel('任务输入', {exact: true}).fill(chatDraft);
    await page.getByTestId('race-nav').click();
    await page.getByTestId('race-panel').waitFor();
    await page.getByTestId('race-prompt').fill(raceDraft);
    const chineseStart = await page.getByTestId('race-start').innerText();
    await page.locator('.language-picker select').selectOption('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    assert.notEqual(await page.getByTestId('race-start').innerText(), chineseStart, 'the app-wide language preference must change race text');
    assert.equal(await page.getByTestId('race-prompt').inputValue(), raceDraft);
    await page.locator('.language-picker select').selectOption('zh');
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
    assert.equal(await page.getByTestId('race-start').innerText(), chineseStart);
    assert.equal(await page.getByTestId('race-prompt').inputValue(), raceDraft);
    await page.getByTestId('chat-view-nav').click();
    assert.equal(await page.getByLabel('任务输入', {exact: true}).inputValue(), chatDraft);
    await page.getByTestId('race-nav').click();
    assert.equal(await page.getByTestId('race-prompt').inputValue(), raceDraft);
  });

  await check('duplicate model selections are blocked by both UI and backend', async () => {
    for (const [index, model] of models.entries()) await page.getByTestId(`race-model-${index}`).selectOption(model);
    await page.getByTestId('race-model-1').selectOption(models[0]);
    assert.equal(await page.getByTestId('race-start').isDisabled(), true);
    const count = await page.evaluate(async () => (await window.rocky.listRaces()).races.length);
    const duplicateModels = await page.evaluate(async input => {
      try { await window.rocky.startRace(input); return {rejected: false}; }
      catch (error) { return {rejected: true, message: String(error)}; }
    }, {prompt: 'Repeated models must not create a race.', models: [models[0], models[0], ...models.slice(2)], limits});
    assert.equal(duplicateModels.rejected, true, 'the backend must require five distinct models');
    assert.equal(await page.evaluate(async () => (await window.rocky.listRaces()).races.length), count);
    await page.getByTestId('race-model-1').selectOption(models[1]);
  });

  let completed;
  await check('five distinct fixture lanes finish through the renderer and IPC', async () => {
    const started = await startFromUI('Compare five offline answers using the same frozen input.' + ' Use the same reviewed task context.'.repeat(8));
    completed = await waitForRace(started.id, 'complete');
    for (const alias of aliases) await waitForLane(alias, 'complete');
    assert.ok(completed.lanes.every(lane => lane.content.length > 0 && lane.error === null), 'every lane must finish with output and no error: ' + JSON.stringify(completed.lanes.map(lane => ({alias: lane.alias, status: lane.status, content: lane.content.length, error: lane.error}))));
    assert.equal(completed.judge.status, 'idle', 'finishing a race must not automatically call a judge');
    assert.equal(completed.publication, 'local-only');
    await page.screenshot({path: join(artifacts, 'race-complete.png')});
  });

  await check('the finish board orders the lanes by time and keeps the models anonymous', async () => {
    const board = page.getByTestId('race-finish');
    await page.waitForFunction(() => document.querySelector('[data-testid="race-finish"]')?.getAttribute('data-over') === 'true');
    const times = [...new Set(completed.lanes.map(lane => lane.elapsedMs))].sort((a, b) => a - b);
    for (const lane of completed.lanes) {
      assert.equal(await page.getByTestId(`race-finish-${lane.alias}`).getAttribute('data-place'), String(times.indexOf(lane.elapsedMs) + 1), `place of lane ${lane.alias}`);
    }
    assert.equal(await board.locator('.race-finish-first').count() >= 1, true, 'the fastest lane is marked');
    assert.equal(await board.locator('.race-finish-name').count(), 0, 'no model name before the human review or an explicit reveal');
    const labels = (await page.evaluate(() => window.rocky.listRaces())).models.map(model => model.label);
    const text = await board.innerText();
    for (const label of labels) assert.ok(!text.includes(label), `the board named a model before the reveal: ${label}`);
    assert.equal(await board.locator('.race-finish-rows').getAttribute('data-run'), '0');
    await page.getByTestId('race-replay').click();
    assert.equal(await board.locator('.race-finish-rows').getAttribute('data-run'), '1', 'Replay runs the flags again');
    await board.screenshot({path: join(artifacts, 'race-finish.png')});
  });

  let savedHuman;
  await check('human scoring supports abstention and does not invoke the judge', async () => {
    for (const alias of aliases) {
      await page.getByTestId(`race-human-score-${alias}`).fill(alias === 'A' ? '1' : alias === 'B' ? '4' : '');
      await page.getByTestId(`race-human-reason-${alias}`).fill(alias === 'A' ? 'Independent human assessment.' : '');
    }
    await page.getByTestId('race-save-human').click();
    const scored = await waitForRaceState(completed.id, race => race.human.find(score => score.alias === 'A')?.reason === 'Independent human assessment.', 'saved human scores');
    savedHuman = scored.human;
    assert.equal(savedHuman.find(score => score.alias === 'A')?.score, 1);
    assert.equal(savedHuman.find(score => score.alias === 'B')?.score, 4);
    for (const alias of ['C', 'D', 'E']) assert.equal(savedHuman.find(score => score.alias === alias)?.score, null);
    assert.equal(scored.judge.status, 'idle');
    assert.deepEqual(scored.judge.scores, []);
    // Saving the human review reveals who ran in which lane, on the finish board too.
    const registry = await page.evaluate(() => window.rocky.listRaces());
    for (const lane of scored.lanes) {
      const model = registry.models.find(entry => entry.id === lane.model);
      const shown = page.getByTestId(`race-finish-${lane.alias}`).locator('.race-finish-name');
      await shown.waitFor();
      assert.equal(await shown.innerText(), model.model, 'the board shows the model name');
      assert.equal(await shown.getAttribute('title'), model.label, 'the full label is the tooltip');
    }
    // Names have different lengths; the finish line stays one line.
    const ends = await page.getByTestId('race-finish').evaluate(board => {
      const names = [...board.querySelectorAll('.race-finish-name')];
      const kept = names.map(node => node.textContent);
      names.forEach((node, index) => { node.textContent = 'a-long-model-name-'.repeat(index * 2 + 1); });
      const found = [...board.querySelectorAll('.race-finish-rail')].map(rail => Math.round(rail.getBoundingClientRect().right));
      names.forEach((node, index) => { node.textContent = kept[index]; });
      return found;
    });
    assert.equal(new Set(ends).size, 1, 'every rail ends at the same place: ' + ends);
    await page.getByTestId('race-finish').screenshot({path: join(artifacts, 'race-finish-revealed.png')});
  });

  await check('manual fixture judge preserves independent human scores', async () => {
    await page.getByTestId('race-judge-model').selectOption('fixture:judge');
    await page.getByTestId('race-run-judge').click();
    const judged = await waitForRaceState(completed.id, race => race.judge.status === 'complete', 'judge complete');
    assert.equal(judged.judge.model, 'fixture:judge');
    assert.equal(judged.judge.scores.length, 5);
    assert.deepEqual(judged.human, savedHuman);
    assert.notDeepEqual(judged.judge.scores, savedHuman, 'judge and human scores must remain separate records');
    await page.getByTestId('race-judge-scores').waitFor();
  });

  await check('history pages contain lightweight summaries and details load separately', async () => {
    const history = await page.evaluate(() => window.rocky.listRaces());
    assert.ok(history.races.length > 0 && history.races.length <= 20);
    assert.equal(history.nextCursor, null, 'this isolated fixture has fewer than one page of races');
    const summary = history.races.find(race => race.id === completed.id);
    assert.ok(summary);
    assert.equal(summary.detailsLoaded, false);
    assert.equal(summary.prompt, completed.prompt.slice(0, 200));
    assert.equal(summary.prompt.length, 200, 'history must truncate long task text');
    for (const lane of summary.lanes) {
      assert.equal(lane.content, '');
      assert.deepEqual(lane.files, []);
      assert.deepEqual(lane.tools, []);
    }
    assert.deepEqual(summary.human, []);
    assert.deepEqual(summary.judge.scores, []);
    const detailed = await readRace(completed.id);
    assert.equal(detailed.prompt, completed.prompt);
    assert.ok(detailed.lanes.every(lane => lane.content.length > 0));
    assert.deepEqual(detailed.human, savedHuman);
    assert.equal(detailed.judge.scores.length, 5);
    const afterLast = await page.evaluate(cursor => window.rocky.listRaces(cursor), history.races.at(-1).id);
    assert.deepEqual(afterLast.races, []);
    assert.equal(afterLast.nextCursor, null);
  });

  await check('reviewed preview digest is enforced and native export cancellation is safe', async () => {
    await app.evaluate(({dialog}) => {
      globalThis.__raceE2EDialogCalls = 0;
      dialog.showSaveDialog = async () => {
        globalThis.__raceE2EDialogCalls += 1;
        return {canceled: true};
      };
    });
    await page.getByTestId('race-preview').click();
    await page.getByTestId('race-preview-json').waitFor();
    const preview = await page.evaluate(id => window.rocky.previewRace(id), completed.id);
    assert.equal(preview.digest, createHash('sha256').update(preview.json).digest('hex'));
    assert.ok(preview.warning.length > 0, 'preview must warn the user to review shared contents');
    const visibleJSON = await page.getByTestId('race-preview-json').evaluate(element => 'value' in element ? element.value : element.textContent);
    assert.deepEqual(JSON.parse(visibleJSON), JSON.parse(preview.json));
    assert.equal(await page.getByTestId('race-export').isDisabled(), true, 'export must wait for explicit preview review');
    const invalid = await page.evaluate(async id => {
      try { await window.rocky.exportRace(id, '0'.repeat(64)); return {rejected: false}; }
      catch (error) { return {rejected: true, message: String(error)}; }
    }, completed.id);
    assert.equal(invalid.rejected, true, 'unreviewed digest must be rejected by the backend');
    assert.equal(await app.evaluate(() => globalThis.__raceE2EDialogCalls), 0, 'invalid digest must not open a save dialog');
    await page.getByTestId('race-export-reviewed').check();
    await page.getByTestId('race-export').click();
    await waitForDialogCalls(1);
    await page.getByTestId('race-export-receipt').waitFor();
    assert.match(await page.getByTestId('race-export-receipt').innerText(), /cancelled|已取消/i);
    const cancelled = await page.evaluate(({id, digest}) => window.rocky.exportRace(id, digest), {id: completed.id, digest: preview.digest});
    assert.deepEqual(cancelled, {saved: false});
    await waitForDialogCalls(2);
    assert.equal((await readRace(completed.id)).publication, 'local-only');
    await page.screenshot({path: join(artifacts, 'race-reviewed-preview.png')});
  });

  await check('backend rejects a duplicate start and cancellation retains partial output', async () => {
    const started = await startFromUI('Cancel these five fixture lanes after they begin. [race-slow]');
    const before = await waitForRaceState(started.id, race => race.status === 'running' && race.lanes.some(lane => lane.content.length > 0), 'first partial output');
    // Starting a race brings the lanes into view, and the finish board is already there:
    // five flags waiting at the start, nobody placed, nothing to replay yet.
    await page.waitForFunction(() => document.querySelector('[data-testid="race-status"]')?.getAttribute('data-status') === 'running');
    await page.waitForFunction(() => {
      const box = document.querySelector('[data-testid="race-finish"]')?.getBoundingClientRect();
      return !!box && box.top >= 0 && box.bottom <= window.innerHeight;
    });
    assert.equal(await page.getByTestId('race-finish').getAttribute('data-over'), 'false');
    assert.equal(await page.getByTestId('race-finish').locator('li').count(), 5);
    assert.equal(await page.getByTestId('race-finish').locator('li[data-place=""]').count(), 5, 'nobody has a place while the race runs');
    assert.equal(await page.getByTestId('race-replay').count(), 0);
    await page.getByTestId('race-finish').screenshot({path: join(artifacts, 'race-finish-waiting.png')});
    const count = await page.evaluate(async () => (await window.rocky.listRaces()).races.length);
    const duplicate = await page.evaluate(async input => {
      try { await window.rocky.startRace(input); return {rejected: false}; }
      catch (error) { return {rejected: true, message: String(error)}; }
    }, {prompt: 'This duplicate must not start.', models, limits});
    assert.equal(duplicate.rejected, true, 'a second active race must be rejected in the backend');
    assert.equal(await page.evaluate(async () => (await window.rocky.listRaces()).races.length), count);
    await page.getByTestId('race-cancel').click();
    const cancelled = await waitForRace(started.id, 'cancelled');
    assert.ok(cancelled.lanes.every(lane => !['queued', 'running'].includes(lane.status)));
    assert.ok(cancelled.lanes.some(lane => lane.status === 'cancelled'));
    await page.waitForFunction(() => !document.querySelector('[data-testid="race-finish"]'));  // nobody finished: no order to show
    for (const lane of before.lanes) assert.ok(cancelled.lanes.find(result => result.id === lane.id).content.startsWith(lane.content), 'cancellation must retain generated content');
    assert.equal(cancelled.judge.status, 'idle');
  });

  let partial;
  await check('one fixture lane failure leaves the other four usable', async () => {
    const started = await startFromUI('Keep independent lanes usable after one failure. [race-fail]');
    partial = await waitForRace(started.id, 'complete');
    // Blind aliases are randomized, so the third selected model is not always C.
    const failed = partial.lanes.find(lane => lane.model === 'fixture:model-3');
    await waitForLane(failed.alias, 'error');
    for (const lane of partial.lanes.filter(lane => lane.id !== failed.id)) await waitForLane(lane.alias, 'complete');
    assert.equal(partial.lanes.filter(lane => lane.status === 'error').length, 1);
    assert.ok(failed.error);
    assert.ok(partial.lanes.filter(lane => lane.id !== failed.id).every(lane => lane.content.length > 0));
    // A lane that failed has no place on the finish board, however fast it failed.
    const out = page.getByTestId(`race-finish-${failed.alias}`);
    await out.waitFor();
    assert.equal(await out.getAttribute('data-place'), '');
    assert.match(await out.getAttribute('class'), /race-finish-out/);
    assert.equal(await page.getByTestId('race-finish').locator('li:not(.race-finish-out)').count(), 4);
    await page.screenshot({path: join(artifacts, 'race-partial-failure.png')});
  });

  await check('partial-race judgment stays hidden until explicitly revealed', async () => {
    await page.getByTestId('race-judge-model').selectOption('fixture:judge');
    await page.getByTestId('race-run-judge').click();
    await page.waitForFunction(() => document.querySelector('[data-testid="race-judge-status"]')?.getAttribute('data-status') === 'complete');
    const judged = await readRace(partial.id);
    assert.deepEqual(judged.human, []);
    assert.deepEqual(judged.judge.scores.map(score => score.alias).sort(), partial.lanes.filter(lane => lane.status === 'complete').map(lane => lane.alias).sort());
    assert.equal(await page.getByTestId('race-judge-scores').count(), 0, 'judge scores must not bias an unsaved human assessment');
    await page.getByTestId('race-reveal-judge').click();
    await page.getByTestId('race-judge-scores').waitFor();
    assert.deepEqual((await readRace(partial.id)).human, [], 'revealing results is not a human vote');
  });

  await check('crashed fixture runtime recovers an interrupted race without replaying it', async () => {
    const started = await startFromUI('Persist an active fixture race before restart. [race-slow]');
    const before = await waitForRaceState(started.id, race => race.status === 'running' && race.lanes.some(lane => lane.content.length > 0), 'first partial output');
    // Abruptly stop only the isolated fixture Python child. A normal app close
    // deliberately cancels work, so it cannot exercise crash recovery.
    const killed = await app.evaluate(() => {
      const runtimes = process._getActiveHandles().filter(handle =>
        handle.constructor?.name === 'ChildProcess' && handle.spawnargs?.includes('rockycode_desktop') && handle.spawnargs.includes('--fixture'));
      if (runtimes.length !== 1) throw new Error('Expected exactly one fixture Python child; cannot test crash recovery safely.');
      return runtimes[0].kill('SIGKILL');
    });
    assert.equal(killed, true);
    await app.close();
    app = null;
    await launch();
    const recovered = await waitForRace(started.id, 'interrupted');
    assert.ok(recovered.lanes.every(lane => !['queued', 'running'].includes(lane.status)));
    assert.ok(recovered.lanes.some(lane => lane.status === 'interrupted'));
    for (const lane of before.lanes) assert.ok(recovered.lanes.find(result => result.id === lane.id).content.startsWith(lane.content));
    assert.deepEqual((await readRace(completed.id)).human, savedHuman, 'human scores must survive restart');
    assert.equal((await readRace(completed.id)).judge.status, 'complete');
    await page.getByTestId('race-nav').click();
    await page.getByTestId('race-history').waitFor();
    await page.getByTestId('race-history').selectOption(started.id);
    for (const lane of recovered.lanes) await waitForLane(lane.alias, lane.status);
    const restoredOutput = recovered.lanes.find(lane => lane.content.length > 0);
    await page.getByTestId(`race-lane-${restoredOutput.alias}`).filter({hasText: restoredOutput.content}).waitFor();
    assert.equal((await readRace(started.id)).status, 'interrupted', 'opening history must not replay interrupted work');
    await page.screenshot({path: join(artifacts, 'race-restart.png')});
  });

  report.result = 'passed';
  console.log('Fixture only: no paid model calls, live model IDs, uploads, or real agent tools. Data: ' + data);
} catch (error) {
  report.result = 'failed';
  report.failedCheck = currentCheck;
  report.error = String(error?.message || error);
  await page?.screenshot({path: join(artifacts, 'race-failure.png')}).catch(() => {});
  if (page && !page.isClosed()) console.error('Visible error:', await page.locator('[role="alert"]').allTextContents().catch(() => []));
  throw error;
} finally {
  await app?.close();
  await writeFile(join(artifacts, 'race-e2e.json'), JSON.stringify(report, null, 2) + '\n');
}
