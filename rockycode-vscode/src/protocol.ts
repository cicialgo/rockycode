/**
 * JSON-RPC 2.0 protocol types for rockycode serve communication.
 */

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id?: string | number;
  method: string;
  params?: Record<string, unknown>;
}

export interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: string | number;
  result?: unknown;
  error?: { code: number; message: string };
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params: Record<string, unknown>;
}

// ── response payloads ──────────────────────────────────────────────────

export interface InitializeResult {
  version: string;
  session_id: string;
  model: string;
}

export interface ChatResult {
  session_id: string;
}

export interface CancelResult {
  cancelled: boolean;
}

export interface ListSessionsResult {
  sessions: SessionInfo[];
}

export interface SessionInfo {
  session_id: string;
  created_at: string;
  n_messages: number;
  running: boolean;
}

export interface StatusResult {
  session_id: string;
  state: 'idle' | 'busy';
}

// ── notification payloads ──────────────────────────────────────────────

export interface StateChangedParams {
  session_id: string;
  state: string;
}

export interface ThinkingDeltaParams {
  session_id: string;
  text: string;
}

export interface TextDeltaParams {
  session_id: string;
  text: string;
}

export interface ToolStartedParams {
  session_id: string;
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
}

export interface ToolFinishedParams {
  session_id: string;
  call_id: string;
  tool: string;
  output: string;
  ok: boolean;
  duration_s: number;
}

export interface CompactedParams {
  session_id: string;
  strategy: string;
  tokens_before: number;
  tokens_after: number;
}

export interface TurnFinishedParams {
  session_id: string;
  steps: number;
  usage: Record<string, number>;
}

export interface ErrorParams {
  session_id: string;
  message: string;
}

export interface PermissionRequestParams {
  session_id: string;
  event_id: string;
  tool: string;
  args: Record<string, unknown>;
  risk: string;
}
