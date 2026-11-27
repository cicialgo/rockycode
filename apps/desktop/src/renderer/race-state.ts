import type {Race, RaceLane, RaceLimits, RaceModel, RaceScore} from '../shared/race';

export const raceAliases = ['A', 'B', 'C', 'D', 'E'] as const;
export interface DraftScore {score: string; reason: string}
export const raceIsRunning = (race?: Race) => !!race && (race.status === 'running' || race.status === 'cancelling');
export const blocksNewRace = (race: Race) => raceIsRunning(race) || race.judge.status === 'running';
export const reviewIsRevealed = (race: Race | undefined, explicitlyRevealed: boolean) => !!race?.human.length || explicitlyRevealed;

/** Provider/model identity matters: two configuration aliases are not two LLMs. */
export function hasDuplicateModels(choices: string[], models: RaceModel[]) {
  const selected = choices.filter(Boolean).map(id => {
    const model = models.find(entry => entry.id === id);
    return model ? JSON.stringify([model.provider, model.model]) : id;
  });
  return new Set(selected).size !== selected.length;
}

export function validRaceLimits(limits: RaceLimits) {
  return Number.isInteger(limits.maxSteps) && limits.maxSteps >= 1 && limits.maxSteps <= 12 &&
    Number.isInteger(limits.maxTokens) && limits.maxTokens >= 256 && limits.maxTokens <= 4096 &&
    Number.isInteger(limits.timeoutSeconds) && limits.timeoutSeconds >= 10 && limits.timeoutSeconds <= 300;
}

export function restoreHumanDraft(scores: RaceScore[] = []): Record<string, DraftScore> {
  return Object.fromEntries(raceAliases.map(alias => {
    const saved = scores.find(score => score.alias === alias);
    return [alias, {score: saved?.score == null ? '' : String(saved.score), reason: saved?.reason ?? ''}];
  }));
}

export function validHumanDraft(scores: Record<string, DraftScore>) {
  return raceAliases.every(alias => {
    const entry = scores[alias];
    return !!entry && (entry.score === '' || (entry.score.trim() !== '' && Number.isFinite(Number(entry.score)) && Number(entry.score) >= 0 && Number(entry.score) <= 10)) && entry.reason.length <= 1000;
  });
}

export function humanDraftPayload(scores: Record<string, DraftScore>): RaceScore[] {
  if (!validHumanDraft(scores)) throw new Error('Invalid human scores');
  return raceAliases.map(alias => ({alias, score: scores[alias].score === '' ? null : Number(scores[alias].score), reason: scores[alias].reason.trim()}));
}

export function replaceRace(current: Race[], incoming: Race): Race[] {
  const previous = current.find(race => race.id === incoming.id);
  const selected = previous?.detailsLoaded && !incoming.detailsLoaded ? previous : incoming;
  return [selected, ...current.filter(race => race.id !== incoming.id)].sort((a, b) => b.createdAt - a.createdAt);
}

/** Events received while the initial list loads are newer than that snapshot. */
export function mergeLoadedRaces(current: Race[], loaded: Race[]): Race[] {
  const byId = new Map(loaded.map(race => [race.id, race]));
  for (const race of current) {
    const incoming = byId.get(race.id);
    if (race.detailsLoaded || !incoming?.detailsLoaded) byId.set(race.id, race);
  }
  return [...byId.values()].sort((a, b) => b.createdAt - a.createdAt);
}

export interface FinishEntry {
  alias: string; lane: RaceLane;
  /** 1 is the fastest lane that completed. Lanes that did not complete have no place. */
  place: number | null;
  /** How far along the track the lane got: 1 is the finish line. */
  share: number;
  /** When it got there, as a share of the slowest lane's time: the replay's clock. */
  time: number;
}
/** `over` is false while the race still runs: the flags wait at the start and nobody has a place. */
export interface FinishBoard {over: boolean; entries: FinishEntry[]}

/** The finish order of a race, by wall time only. Rows stay in lane order A–E; `place`
 * carries the ranking. Nothing here says which answer is better: a lane that failed fast
 * is not a winner, so only completed lanes are placed. While the race runs the board is
 * already there, with every flag at the start, so the lanes below do not jump when it ends. */
export function finishBoard(race: Race | undefined): FinishBoard | null {
  if (!race?.detailsLoaded || !race.lanes.length) return null;
  const lanes = [...race.lanes].sort((a, b) => a.alias.localeCompare(b.alias));
  if (raceIsRunning(race)) return {over: false, entries: lanes.map(lane => ({alias: lane.alias, lane, place: null, share: 0, time: 0}))};
  const timed = (lane: RaceLane) => lane.elapsedMs !== null && Number.isFinite(lane.elapsedMs) && lane.elapsedMs >= 0;
  const finished = lanes.filter(lane => lane.status === 'complete' && timed(lane));
  if (!finished.length) return null;
  const slowest = Math.max(1, ...lanes.filter(timed).map(lane => lane.elapsedMs as number));
  const times = [...new Set(finished.map(lane => lane.elapsedMs as number))].sort((a, b) => a - b);
  return {over: true, entries: lanes.map(lane => {
    const done = finished.includes(lane);
    const time = timed(lane) ? Math.min(1, Math.max(0.08, (lane.elapsedMs as number) / slowest)) : 0.08;
    return {alias: lane.alias, lane, place: done ? times.indexOf(lane.elapsedMs as number) + 1 : null,
      share: done ? 1 : Math.min(0.85, time), time};
  })};
}
