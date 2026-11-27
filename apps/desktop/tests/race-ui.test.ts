import test from 'node:test';
import assert from 'node:assert/strict';
import {blocksNewRace, finishBoard, hasDuplicateModels, humanDraftPayload, mergeLoadedRaces, replaceRace, restoreHumanDraft, reviewIsRevealed, validHumanDraft, validRaceLimits} from '../src/renderer/race-state';
import type {Race, RaceLane, RaceModel} from '../src/shared/race';

const models: RaceModel[] = [
  {id: 'a', provider: 'provider-a', model: 'model-a', label: 'A'},
  {id: 'a-alias', provider: 'provider-a', model: 'model-a', label: 'Same LLM'},
  {id: 'b', provider: 'provider-b', model: 'model-a', label: 'B'},
];
const race = (id = 'race-1', status: Race['status'] = 'complete', createdAt = 1): Race => ({id, detailsLoaded: true, prompt: 'Compare', status, createdAt, limits: {maxSteps: 6, maxTokens: 2048, timeoutSeconds: 120}, fixture: true, lanes: [], snapshotHash: 'test', includedFiles: 0, excludedFiles: 0, judge: {status: 'idle', model: null, scores: [], error: null}, human: [], publication: 'local-only'});

test('race UI detects duplicate canonical models, not only selector IDs', () => {
  assert.equal(hasDuplicateModels(['a', 'a'], models), true);
  assert.equal(hasDuplicateModels(['a', 'a-alias'], models), true);
  assert.equal(hasDuplicateModels(['a', 'b'], models), false);
  assert.equal(hasDuplicateModels(['', '', 'a'], models), false);
});

test('race UI uses whole-number backend limits and rejects empty or non-finite input', () => {
  const defaults = race().limits;
  assert.equal(validRaceLimits(defaults), true);
  assert.equal(validRaceLimits({maxSteps: 1, maxTokens: 256, timeoutSeconds: 10}), true);
  assert.equal(validRaceLimits({maxSteps: 12, maxTokens: 4096, timeoutSeconds: 300}), true);
  for (const [key, values] of Object.entries({maxSteps: [0, 13, 1.5, NaN], maxTokens: [255, 4097, Infinity], timeoutSeconds: [9, 301, NaN]})) {
    for (const value of values) assert.equal(validRaceLimits({...defaults, [key]: value}), false, `${key}: ${value}`);
  }
});

test('human draft restores zero and abstention distinctly without changing source scores', () => {
  const saved = [{alias: 'A', score: 0, reason: 'No evidence'}, {alias: 'B', score: null, reason: 'Abstain'}];
  const before = structuredClone(saved);
  const draft = restoreHumanDraft(saved);
  assert.equal(draft.A.score, '0');
  assert.equal(draft.B.score, '');
  assert.deepEqual(draft.E, {score: '', reason: ''});
  assert.equal(validHumanDraft(draft), true);
  const payload = humanDraftPayload(draft);
  assert.equal(payload[0].score, 0);
  assert.equal(payload[1].score, null);
  assert.equal(payload.length, 5);
  assert.deepEqual(saved, before);
});

test('human score validation matches 0–10 or abstain and 1000-character reason', () => {
  const draft = restoreHumanDraft();
  for (const value of ['0', '10', '7.5', '']) {
    draft.A.score = value;
    assert.equal(validHumanDraft(draft), true);
  }
  for (const value of ['-1', '11', 'NaN', 'Infinity', ' ']) {
    draft.A.score = value;
    assert.equal(validHumanDraft(draft), false);
    assert.throws(() => humanDraftPayload(draft));
  }
  draft.A.score = '8';
  draft.A.reason = 'a'.repeat(1000);
  assert.equal(validHumanDraft(draft), true);
  draft.A.reason += 'a';
  assert.equal(validHumanDraft(draft), false);
});

test('judge visibility requires saved human scores or a deliberate reveal', () => {
  const item = race();
  item.judge = {status: 'complete', model: 'b', scores: [{alias: 'A', score: 8, reason: 'Useful'}], error: null};
  assert.equal(reviewIsRevealed(item, false), false);
  assert.equal(reviewIsRevealed(item, true), true);
  item.human = humanDraftPayload(restoreHumanDraft());
  assert.equal(reviewIsRevealed(item, false), true);
});

test('active judging also blocks a new race; terminal partial results do not', () => {
  assert.equal(blocksNewRace(race('a', 'running')), true);
  assert.equal(blocksNewRace(race('a', 'cancelling')), true);
  for (const status of ['complete', 'cancelled', 'interrupted'] as const) assert.equal(blocksNewRace(race('a', status)), false);
  const judged = race(); judged.judge.status = 'running';
  assert.equal(blocksNewRace(judged), true);
});

test('history merge preserves newer live events and immutable local selection records', () => {
  const current = [race('latest', 'complete', 3), race('older', 'interrupted', 1)];
  const loaded = [race('latest', 'running', 3), race('middle', 'complete', 2)];
  const combined = mergeLoadedRaces(current, loaded);
  assert.deepEqual(combined.map(item => item.id), ['latest', 'middle', 'older']);
  assert.equal(combined[0].status, 'complete');
  const next = replaceRace(combined, {...combined[1], human: humanDraftPayload(restoreHumanDraft())});
  assert.equal(next[1].human.length, 5);
  assert.equal(combined[1].human.length, 0);
  assert.equal(current.length, 2);
});


test('paged history never replaces loaded details or duplicates overlapping rows', () => {
  const full = race('one', 'complete', 3);
  full.human = [{alias: 'A', score: 9, reason: 'Saved review'}];
  full.prompt = 'Full original prompt';
  const summary = {...full, detailsLoaded: false, human: [], prompt: 'Short prompt'};
  const older = {...race('two', 'complete', 1), detailsLoaded: false};
  const result = mergeLoadedRaces([full], [summary, older, older]);
  assert.deepEqual(result.map(item => item.id), ['one', 'two']);
  assert.equal(result[0], full);
  assert.equal(result[0].human[0].score, 9);
  assert.equal(replaceRace([full], summary)[0], full);
  assert.equal(mergeLoadedRaces([summary], [full])[0], full);
  assert.equal(replaceRace([summary], full)[0], full);
});

const lane = (alias: string, status: RaceLane['status'], elapsedMs: number | null): RaceLane => ({id: `lane-${alias}`, alias, model: `model-${alias}`, status, content: '', error: null, tools: [], elapsedMs, inputTokens: null, outputTokens: null, costUsd: null, files: []});

test('the finish board places completed lanes by time and never ranks a lane that did not finish', () => {
  const finished = race();
  // Given out of order on purpose: rows stay A–E, the place carries the ranking.
  finished.lanes = [lane('C', 'complete', 9000), lane('A', 'complete', 3000), lane('E', 'error', 1000), lane('B', 'complete', 12000), lane('D', 'cancelled', null)];
  const {over, entries: board} = finishBoard(finished)!;
  assert.equal(over, true);
  assert.deepEqual(board.map(entry => entry.alias), ['A', 'B', 'C', 'D', 'E']);
  assert.deepEqual(board.map(entry => entry.place), [1, 3, 2, null, null], 'E failed in one second: fast, and not a winner');
  assert.deepEqual(board.map(entry => entry.share), [1, 1, 1, 0.08, 1000 / 12000], 'only a completed lane reaches the line');
  assert.deepEqual(board.map(entry => entry.time), [0.25, 1, 0.75, 0.08, 1000 / 12000], 'the fastest arrives first: its share of the slowest time');
  assert.ok(board.every(entry => entry.share > 0 && entry.share <= 1 && entry.time > 0 && entry.time <= 1));
});

test('the finish board waits at the start while the race runs, and needs a lane that completed', () => {
  const lanes = [lane('C', 'complete', 4000), lane('A', 'complete', 3000), lane('B', 'complete', 3000)];
  for (const status of ['running', 'cancelling'] as const) {
    // Lane A is already done, and still has no place: the order is shown when the race is over.
    const waiting = finishBoard({...race('r', status), lanes: [lane('B', 'running', null), lane('A', 'complete', 900)]})!;
    assert.equal(waiting.over, false, status);
    assert.deepEqual(waiting.entries.map(entry => [entry.alias, entry.place, entry.share]), [['A', null, 0], ['B', null, 0]], status);
  }
  assert.equal(finishBoard(undefined), null);
  assert.equal(finishBoard({...race(), detailsLoaded: false, lanes}), null, 'a history summary has no lane times yet');
  assert.equal(finishBoard(race()), null, 'no lanes, no board');
  assert.equal(finishBoard({...race(), lanes: [lane('A', 'error', 500), lane('B', 'cancelled', null)]}), null, 'nobody finished: no order to show');
  for (const status of ['complete', 'cancelled', 'interrupted'] as const) assert.equal(finishBoard({...race('r', status), lanes})!.over, true, status);
  assert.deepEqual(finishBoard({...race(), lanes})!.entries.map(entry => [entry.alias, entry.place]), [['A', 1], ['B', 1], ['C', 2]], 'the same time is the same place');
  // A lane that ran longer than every finisher and then failed stops short of the line.
  const late = finishBoard({...race(), lanes: [lane('A', 'complete', 2000), lane('B', 'error', 9000)]})!;
  assert.deepEqual(late.entries.map(entry => [entry.place, entry.share]), [[1, 1], [null, 0.85]]);
});
