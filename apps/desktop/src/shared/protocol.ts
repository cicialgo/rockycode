import type {LanguagePreference} from './i18n';
import type {ThemePreference} from './theme';
import type {PromptPreset} from './presets';
import type {RaceAPI, Race} from './race';
import type {UpdateCheck} from './updates';

export type ThreadStatus = 'idle' | 'running' | 'waiting' | 'cancelling' | 'error' | 'interrupted';
export type MessageStatus = 'streaming' | 'complete' | 'cancelled' | 'error' | 'interrupted';
/** `decision` is set when the approval pipeline decided without asking: a static block, the verifier's auto allow/deny, or a step yolo parked for the user. */
export interface KeyProvider { id: string; label: string; host: string; configured: boolean; place: 'keychain' | 'file' | 'shell' | '' }
export interface ToolCall { id: string; name: string; status: string; output: string; /** What the call acts on: the command, the file, the query. */ detail?: string; decision?: 'blocked' | 'auto_allowed' | 'auto_denied' | 'parked' }
/** `error` is English text; `error_key` (+ `error_params`) is the desktop catalog key the client renders when it knows it. */
export interface Message { id: string; role: 'user' | 'assistant'; content: string; reasoning?: string; status: MessageStatus; tools: ToolCall[]; error?: string; error_key?: string | null; error_params?: Record<string, string | number> }
export interface Approval { id: string; tool: string; args: Record<string, unknown>; sessionLabel?: string | null; warning?: string | null; expiresAt?: number }
export type ApprovalDecision = 'once' | 'session' | 'deny';
/** A step only the user can allow, which yolo refused instead of waiting. `detail` is the step in plain words, secrets masked. */
export interface ParkedStep { id: string; tool: string; detail: string; warning?: string | null; at: number }
export type ParkedDecision = 'run' | 'skip';
export interface ModelChoice { id: string; label: string; configured: boolean; vision: boolean }
export interface Capability { name: string; status: string; detail: string; detail_key?: string | null; params?: Record<string, string | number> }
export type DeliveryMode = 'queue' | 'interrupt';
export type PermissionMode = 'careful' | 'ask' | 'yolo';
export interface PendingMessage { id: string; prompt: string }
export interface Artifact {path: string; title: string}
export interface ArtifactPreview extends Artifact {url: string}
export interface TaskMail {id: string; sender: string; recipient: string; content: string; created: number}
export interface Thread { parentTaskId?: string | null; taskInbox?: TaskMail[]; id: string; title: string; workspace: string; updatedAt: number; messages: Message[]; note: string; draft: string; status: ThreadStatus; runId: string | null; approval: Approval | null; parked?: ParkedStep[]; tools: string[]; capabilities?: Capability[]; delivery?: {pending: PendingMessage[]; paused: boolean}; permissionMode?: PermissionMode; model?: string; artifacts?: Artifact[] }
/** `limitations` are desktop catalog keys (`diagnostic.limitation*`); `error_key` localizes `error`. */
export interface RuntimeInfo { mode: 'live' | 'fixture'; available: boolean; model: string; version: string; limitations: string[]; error?: string | null; error_key?: string | null; /** The exact terminal command that opens the first-run setup. */ setup_command?: string; /** Keys the system keychain should hold and did not give (names only). */ keychain_silent?: string[] }
export interface Snapshot { protocolVersion: number; runtime: RuntimeInfo; threads: Thread[]; presets?: PromptPreset[] }
export type RuntimeEvent =
  | {type: 'race'; race: Race}
  | { type: 'thread'; threadId: string; runId: string | null; thread: Thread }
  | { type: 'delta'; threadId: string; runId: string; messageId: string; field: 'content' | 'reasoning'; delta: string }
  | { type: 'disconnected'; error: string }
  /** A manual update check started from the app menu. */
  | { type: 'update'; manual: boolean; result: UpdateCheck };
export interface LanguageSettings { preference: LanguagePreference; systemLocale: string }
export interface DesktopAPI extends RaceAPI {
  getLanguage(): Promise<LanguageSettings>;
  setLanguage(preference: LanguagePreference): Promise<LanguageSettings>;
  getTheme(): Promise<ThemePreference>;
  setTheme(theme: ThemePreference): Promise<ThemePreference>;
  /** The update reminder: the launch check's result, the off switch, a manual check, "later", and the one link it may open. */
  launchUpdateCheck(): Promise<UpdateCheck>;
  getUpdateSettings(): Promise<{enabled: boolean}>;
  setUpdateChecks(enabled: boolean): Promise<{enabled: boolean}>;
  checkForUpdate(): Promise<UpdateCheck>;
  skipUpdate(version: string): Promise<void>;
  openUpdate(url: string): Promise<void>;
  initialize(): Promise<Snapshot>;
  chooseWorkspace(): Promise<string | null>;
  createThread(workspace: string): Promise<Thread>;
  dismissTaskMessage(threadId: string, messageId: string): Promise<{removed: boolean}>;
  createTask(threadId: string, prompt: string): Promise<Thread>;
  sendTaskMessage(threadId: string, target: string, message: string): Promise<{message_id: string; status: string}>;
  startTurn(threadId: string, prompt: string, delivery?: DeliveryMode): Promise<{runId: string | null; message_id?: string; status?: string}>;
  cancelTurn(threadId: string): Promise<{cancelled: boolean}>;
  resumeQueue(threadId: string): Promise<unknown>;
  removeQueued(threadId: string, messageId: string): Promise<unknown>;
  respondApproval(threadId: string, approvalId: string, decision: ApprovalDecision): Promise<unknown>;
  /** Decide a parked step. `prompt` (for 'run') is the sentence that tells Rocky to pick it up, in the user's language. */
  decideParked(threadId: string, stepId: string, decision: ParkedDecision, prompt?: string): Promise<{parked: number}>;
  setPermissionMode(threadId: string, mode: PermissionMode): Promise<unknown>;
  renameThread(threadId: string, title: string): Promise<unknown>;
  deleteThread(threadId: string): Promise<unknown>;
  restartRuntime(): Promise<Snapshot>;
  previewArtifact(threadId: string, path: string): Promise<ArtifactPreview>;
  openArtifactExternal(threadId: string, path: string): Promise<void>;
  closeArtifactPreview(url: string): Promise<void>;
  listModels(): Promise<{model: string; models: ModelChoice[]}>;
  /** The endpoints a key can be added for, and whether each has one. */
  keyProviders(): Promise<{providers: KeyProvider[]; keychain: boolean}>;
  /** Test and save one provider key. `verified` is false when the provider could not be reached to test it. */
  saveKey(endpoint: string, key: string, keychain: boolean): Promise<{runtime: RuntimeInfo; verified: boolean; place: 'keychain' | 'file'}>;
  setModel(threadId: string, model: string): Promise<{model: string}>;
  saveDraft(threadId: string, content: string): Promise<unknown>;
  saveNote(threadId: string, content: string): Promise<unknown>;
  listPresets(): Promise<{presets: PromptPreset[]}>;
  savePreset(input: {id?: string; name: string; body: string}): Promise<{preset: PromptPreset; presets: PromptPreset[]}>;
  deletePreset(id: string): Promise<{deleted: boolean; presets: PromptPreset[]}>;
  onEvent(listener: (event: RuntimeEvent) => void): () => void;
}
export const isActive = (status: ThreadStatus) => ['running', 'waiting', 'cancelling'].includes(status);
