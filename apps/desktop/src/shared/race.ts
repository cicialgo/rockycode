export interface RaceModel { id: string; label: string; provider: string; model: string }
export interface RaceLimits { maxSteps: number; maxTokens: number; timeoutSeconds: number }
export type RaceStatus = 'running' | 'cancelling' | 'complete' | 'cancelled' | 'interrupted';
export interface RaceLane {
  id: string; alias: string; model: string;
  status: 'queued' | 'running' | 'complete' | 'cancelled' | 'error' | 'interrupted';
  content: string; error: string | null; error_key?: string | null; tools: {name: string; status: string}[];
  elapsedMs: number | null; inputTokens: number | null; outputTokens: number | null; costUsd: number | null;
  files: {path: string; content: string; truncated?: boolean; omittedFiles?: number}[];
}
export interface RaceScore { alias: string; score: number | null; reason: string }
export interface Race {
  id: string; detailsLoaded: boolean; prompt: string; status: RaceStatus; createdAt: number; limits: RaceLimits;
  fixture: boolean; lanes: RaceLane[]; snapshotHash: string; includedFiles: number; excludedFiles: number;
  judge: {status: 'idle' | 'running' | 'complete' | 'error' | 'cancelled' | 'interrupted'; model: string | null; scores: RaceScore[]; error: string | null; error_key?: string | null};
  human: RaceScore[]; publication: 'local-only';
}
export interface RaceStart { prompt: string; models: string[]; workspace?: string | null; limits: RaceLimits }
export interface RaceAPI {
  listRaces(cursor?: string): Promise<{races: Race[]; models: RaceModel[]; fixture: boolean; nextCursor: string | null}>;
  getRace(id: string): Promise<Race>;
  startRace(input: RaceStart): Promise<Race>;
  cancelRace(id: string): Promise<unknown>;
  judgeRace(id: string, model: string): Promise<Race>;
  scoreRace(id: string, scores: RaceScore[]): Promise<Race>;
  previewRace(id: string): Promise<{json: string; digest: string; warning: string}>;
  exportRace(id: string, digest: string): Promise<{saved: boolean}>;
}
