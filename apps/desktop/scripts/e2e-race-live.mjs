// Explicit paid Mac/Linux smoke test of the model race: real Electron/IPC/Python/Runtime/providers.
// Five configured models each get a tiny file-writing task, one judge call follows, and the
// reviewed export is saved to a temporary folder. Run after npm run build:
//   npm run test:e2e:race:live -- --live
// --dry-run drives the identical flow against the offline fixture host instead (no paid calls),
// which verifies the script itself before a live run.
// ROCKYCODE_RACE_MODELS=id1,id2,id3,id4,id5 and ROCKYCODE_RACE_JUDGE=id override the automatic
// choice (the first five distinct provider/model pairs the app offers, and the first of those).
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {coreSource, corePython} from './core-paths.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp, mkdir, writeFile, readFile, realpath} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {isAbsolute, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const dryRun = process.argv.includes('--dry-run');
if (!dryRun && !process.argv.includes('--live')) throw new Error('Pass --live to allow paid model calls, or --dry-run for the offline fixture.');
const root = fileURLToPath(new URL('../', import.meta.url));
const core = coreSource();
const python = corePython(core);
if (!core) throw new Error('Shared Runtime not found at the repository root; set ROCKYCODE_SOURCE.');
if (!isAbsolute(python)) throw new Error('Set ROCKYCODE_PYTHON to an absolute interpreter path.');
const data = await realpath(await mkdtemp(join(tmpdir(), 'rockycode-race-live-')));
const state = join(data, 'state');
const artifacts = join(root, 'artifacts');
await mkdir(state, {recursive: true});
await mkdir(artifacts, {recursive: true});
// Keep selectors independent of the host's system locale.
await writeFile(join(state, 'ui-preferences.json'), JSON.stringify({language: 'en'}));
const env = {...process.env, ROCKYCODE_DESKTOP_DEV: '1', ROCKYCODE_SOURCE: core, ROCKYCODE_PYTHON: python, ROCKYCODE_DESKTOP_DATA_DIR: state};
delete env.ELECTRON_RUN_AS_NODE;
delete env.ROCKYCODE_DESKTOP_FIXTURE;
delete env.ROCKYCODE_DESKTOP_TEST;
delete env.ROCKYCODE_DESKTOP_TEST_WORKSPACE;
if (dryRun) Object.assign(env, {ROCKYCODE_DESKTOP_FIXTURE: '1', ROCKYCODE_DESKTOP_TEST: '1'});

const limits = {maxSteps: 4, maxTokens: 512, timeoutSeconds: 120};
const prompt = 'Use write_file to create a file named answer.txt whose entire content is the single line "race ok". Then reply with exactly one word: DONE.';
const report = {mode: dryRun ? 'offline-fixture-dry-run' : 'real-electron-real-api', result: 'running', checks: [], limits};
const scrub = text => String(text).replaceAll(data, '<data>');
let app;
let page;

async function check(name, action) {
  await action();
  report.checks.push(name);
  console.log(`PASS: ${name}`);
}

// Backend state is polled from Node: Playwright's waitForFunction does not await an async predicate.
async function waitForRaceState(id, test, label, timeout) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const race = await page.evaluate(raceId => window.rocky.getRace(raceId), id);
    if (race.detailsLoaded && test(race)) return race;
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${label}: ` + JSON.stringify({status: race.status, judge: race.judge.status, lanes: race.lanes.map(lane => [lane.alias, lane.status, lane.content.length])}));
    await delay(250);
  }
}

const laneSummary = lane => ({alias: lane.alias, model: lane.model, status: lane.status, error: lane.error, elapsedMs: lane.elapsedMs,
  inputTokens: lane.inputTokens, outputTokens: lane.outputTokens, costUsd: lane.costUsd,
  tools: lane.tools.map(tool => `${tool.name}:${tool.status}`), files: lane.files.map(file => file.path ?? file.name ?? file), answer: lane.content.trim().slice(0, 200)});

try {
  app = await electron.launch({executablePath: electronPath(), args: ['.'], cwd: root, env});
  page = await app.firstWindow();
  page.setDefaultTimeout(30_000);
  await page.getByTestId('race-nav').waitFor();
  await page.waitForFunction(() => !document.querySelector('.language-picker select')?.disabled);
  await page.getByTestId('race-nav').click();
  await page.getByTestId('race-panel').waitFor();

  let models;
  await check('the live registry offers five distinct configured models', async () => {
    const registry = await page.evaluate(() => window.rocky.listRaces());
    assert.equal(registry.fixture, dryRun, dryRun ? 'the dry run must use the offline fixture' : 'this run must not use the offline fixture');
    const offered = registry.models;
    const wanted = process.env.ROCKYCODE_RACE_MODELS?.split(',').map(id => id.trim()).filter(Boolean);
    if (wanted) {
      for (const id of wanted) assert.ok(offered.some(model => model.id === id), `ROCKYCODE_RACE_MODELS names an unconfigured model: ${id}`);
      models = wanted;
    } else {
      const seen = new Set();
      models = [];
      for (const model of offered) {
        const pair = `${model.provider}/${model.model}`;
        if (seen.has(pair)) continue;
        seen.add(pair);
        models.push(model.id);
        if (models.length === 5) break;
      }
    }
    assert.equal(models.length, 5, `five distinct configured models are required; the app offers ${offered.length}: ${offered.map(model => model.id).join(', ')}`);
    report.models = models;
    report.offered = offered.map(model => model.id);
  });

  let race;
  await check('a five-model race starts from the renderer with equal limits', async () => {
    await page.getByTestId('race-prompt').fill(prompt);
    for (const [index, model] of models.entries()) await page.getByTestId(`race-model-${index}`).selectOption(model);
    const inputs = page.locator('.race-limits input');
    for (const [index, key] of ['maxSteps', 'maxTokens', 'timeoutSeconds'].entries()) await inputs.nth(index).fill(String(limits[key]));
    assert.equal(await page.getByTestId('race-start').isDisabled(), false, 'start must be enabled for a valid live race');
    const previous = await page.evaluate(async () => (await window.rocky.listRaces()).races.map(race => race.id));
    report.startedAt = new Date().toISOString();
    await page.getByTestId('race-start').click();
    const deadline = Date.now() + 30_000;
    let id = null;
    while (!id) {
      const fresh = await page.evaluate(async () => (await window.rocky.listRaces()).races.map(race => race.id));
      id = fresh.find(raceId => !previous.includes(raceId)) ?? null;
      if (!id && Date.now() > deadline) assert.fail('the started race never appeared in history');
      if (!id) await delay(100);
    }
    race = await page.evaluate(raceId => window.rocky.getRace(raceId), id);
    assert.equal(race.fixture, dryRun);
    assert.deepEqual(race.limits, limits);
    assert.deepEqual([...new Set(race.lanes.map(lane => lane.model))].sort(), [...models].sort());
  });

  await check('all five lanes reach a terminal state and at least one finishes with output', async () => {
    const start = Date.now();
    race = await waitForRaceState(race.id, current => current.status === 'complete', 'race complete', (limits.timeoutSeconds + 60) * 1000);
    report.raceSeconds = (Date.now() - start) / 1000;
    report.lanes = race.lanes.map(laneSummary);
    for (const lane of race.lanes) {
      const shown = await page.getByTestId(`race-lane-${lane.alias}`).getAttribute('data-status');
      assert.equal(shown, lane.status, `lane ${lane.alias} renders its backend status`);
    }
    const finished = race.lanes.filter(lane => lane.status === 'complete');
    report.finishedLanes = finished.length;
    assert.ok(finished.length > 0, 'every lane failed: ' + JSON.stringify(report.lanes));
    assert.ok(finished.every(lane => lane.content.length > 0), 'finished lanes must carry output');
    assert.equal(race.judge.status, 'idle', 'finishing a race must not automatically call a judge');
    await page.screenshot({path: join(artifacts, 'race-live.png')});
  });

  await check('one explicit judge call scores the finished lanes', async () => {
    const judge = process.env.ROCKYCODE_RACE_JUDGE || models[0];
    await page.getByTestId('race-judge-model').selectOption(judge);
    await page.getByTestId('race-run-judge').click();
    race = await waitForRaceState(race.id, current => ['complete', 'error'].includes(current.judge.status), 'judge terminal', 150_000);
    report.judge = {model: judge, status: race.judge.status, error: race.judge.error, scores: race.judge.scores};
    assert.equal(race.judge.status, 'complete', `judge failed: ${race.judge.error}`);
    assert.deepEqual(race.judge.scores.map(score => score.alias).sort(), race.lanes.filter(lane => lane.status === 'complete').map(lane => lane.alias).sort());
    await page.getByTestId('race-reveal-judge').click();
    await page.getByTestId('race-judge-scores').waitFor();
  });

  await check('the reviewed export preview is saved locally with a matching digest', async () => {
    const target = join(data, 'race-export.json');
    await app.evaluate(({dialog}, filePath) => { dialog.showSaveDialog = async () => ({canceled: false, filePath}); }, target);
    await page.getByTestId('race-preview').click();
    await page.getByTestId('race-preview-json').waitFor();
    const preview = await page.evaluate(id => window.rocky.previewRace(id), race.id);
    assert.equal(preview.digest, createHash('sha256').update(preview.json).digest('hex'));
    const exported = JSON.parse(preview.json);
    assert.equal(exported.publication, 'not-uploaded');
    await page.getByTestId('race-export-reviewed').check();
    await page.getByTestId('race-export').click();
    await page.getByTestId('race-export-receipt').waitFor();
    assert.match(await page.getByTestId('race-export-receipt').innerText(), /saved/i);
    const saved = await readFile(target, 'utf8');
    assert.equal(createHash('sha256').update(saved).digest('hex'), preview.digest, 'the saved file must match the reviewed preview byte for byte');
    report.exportBytes = saved.length;
    await page.screenshot({path: join(artifacts, 'race-live-export.png')});
  });

  report.result = 'passed';
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  report.result = 'failed';
  report.error = scrub(error.message);
  await page?.screenshot({path: join(artifacts, 'race-live-failure.png')}).catch(() => {});
  console.error(JSON.stringify(report, null, 2));
  throw error;
} finally {
  await app?.close().catch(() => {});
  await writeFile(join(artifacts, dryRun ? 'race-live-dry-run.json' : 'race-live.json'), JSON.stringify(report, null, 2));
}
