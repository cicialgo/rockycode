/**
 * Manages the rockycode serve child process and JSON-RPC communication
 * over stdin/stdout (NDJSON, one JSON object per line).
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { spawn, ChildProcess } from 'child_process';
import { createInterface, Interface } from 'readline';

type NotificationHandler = (params: Record<string, unknown>) => void;

/**
 * Locate the rockycode CLI. An explicit rockycode.pythonPath wins. Otherwise
 * prefer a project-local virtualenv (how a git-installed rockycode is usually
 * run) before a bare PATH lookup — plain 'rockycode' fails on macOS GUI launches
 * that don't inherit the shell PATH.
 */
function resolveRockycode(configured: string, workdir: string): string {
  if (configured && configured !== 'rockycode') {
    return configured; // user set an explicit path/command — trust it
  }
  const local = [
    path.join(workdir, '.venv', 'bin', 'rockycode'),
    path.join(workdir, '.venv', 'Scripts', 'rockycode.exe'),
    path.join(workdir, 'venv', 'bin', 'rockycode'),
  ];
  for (const p of local) {
    try {
      if (fs.existsSync(p)) return p;
    } catch {
      /* ignore */
    }
  }
  return configured || 'rockycode';
}

export class RockyConnection implements vscode.Disposable {
  private process: ChildProcess | null = null;
  private rl: Interface | null = null;
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }>();
  private handlers = new Map<string, Set<NotificationHandler>>();
  private buffer = '';
  private _sessionId: string | null = null;
  private _model: string | null = null;
  private _disposing = false;
  private _onExit: (() => void) | null = null;
  private _keyHint = '';
  _hasApiKey = false;

  constructor(private _secrets: vscode.SecretStorage) {}

  /** Last 4 chars of the active key — for a masked "••••1234" display. */
  get keyHint(): string { return this._keyHint; }

  /** Register a callback for when serve exits *unexpectedly* (not via dispose). */
  onUnexpectedExit(cb: () => void): void {
    this._onExit = cb;
  }

  // ── lifecycle ────────────────────────────────────────────────────────

  async start(workdir: string): Promise<void> {
    this._disposing = false;
    const cfg = vscode.workspace.getConfiguration('rockycode');
    const pythonPath = resolveRockycode(cfg.get<string>('pythonPath', 'rockycode'), workdir);
    const model = cfg.get<string>('model', 'deepseek-v4-flash');
    // Key comes from encrypted Secret Storage (never plaintext settings). The
    // deprecated rockycode.apiKey setting is a fallback for power users, and
    // serve itself also loads ~/.rockycode/.env, so a missing key here is fine.
    const apiKey = (await this._secrets.get('rockycode.apiKey')) || cfg.get<string>('apiKey', '');
    const baseUrl = cfg.get<string>('baseUrl', '');
    const thinking = cfg.get<boolean>('thinking', true);
    const reasoningEffort = cfg.get<string>('reasoningEffort', 'max');
    const maxTokens = cfg.get<number>('maxTokens', 16384);
    const contextWindow = cfg.get<number>('contextWindow', 131072);
    const systemPrompt = cfg.get<string>('systemPrompt', '');

    const args = ['serve', '--workdir', workdir];
    if (model) {
      args.push('--model', model);
    }

    // Pass config as env vars — rockycode serve reads these directly
    const env = { ...process.env };
    // Priority: VS Code settings > process.env > built-in default
    if (apiKey) env.OPENAI_API_KEY = apiKey;
    if (baseUrl) env.OPENAI_BASE_URL = baseUrl;
    // Always override with VS Code settings if configured
    if (model) env.ROCKYCODE_MODEL = model;
    if (maxTokens > 0) env.ROCKYCODE_MAX_TOKENS = String(maxTokens);
    if (contextWindow > 0) env.ROCKYCODE_CONTEXT_WINDOW = String(contextWindow);
    env.ROCKYCODE_THINKING = thinking ? 'true' : 'false';
    env.ROCKYCODE_REASONING_EFFORT = reasoningEffort;
    if (systemPrompt) env.ROCKYCODE_SYSTEM_PROMPT = systemPrompt;
    // The extension opens artifact URLs itself (from the tool output), so tell
    // serve NOT to also launch a browser — otherwise every artifact opens twice.
    env.ROCKYCODE_ARTIFACT_NO_BROWSER = '1';

    this._hasApiKey = !!(apiKey || process.env.OPENAI_API_KEY);
    const effectiveKey = apiKey || process.env.OPENAI_API_KEY || '';
    this._keyHint = effectiveKey ? effectiveKey.slice(-4) : '';

    this.process = spawn(pythonPath, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      env,
    });

    this.process.stderr?.on('data', (data: Buffer) => {
      console.error(`[rockycode stderr] ${data.toString().trim()}`);
    });

    // A missing binary emits 'error' (ENOENT). Without this handler it becomes an
    // unhandled exception (the "Unexpected SIGPIPE" crash); catch it and surface
    // a fixable message instead.
    this.process.on('error', (err: Error) => {
      console.error(`[rockycode] failed to launch '${pythonPath}': ${err.message}`);
      this.process = null;
      this.rejectAllPending(new Error(
        `Could not launch rockycode ('${pythonPath}'): ${err.message}. ` +
        `Set "rockycode.pythonPath" to your rockycode executable ` +
        `(e.g. <project>/.venv/bin/rockycode).`,
      ));
    });

    this.process.on('exit', (code, signal) => {
      console.log(`[rockycode] process exited (code=${code} signal=${signal})`);
      this.process = null; // stop further writes to a dead pipe (avoids SIGPIPE)
      this.rejectAllPending(new Error('rockycode serve is not connected'));
      if (!this._disposing) {
        this._sessionId = null; // force a fresh handshake on reconnect
        this._onExit?.();
      }
    });

    // Read NDJSON lines from stdout
    this.rl = createInterface({ input: this.process.stdout!, crlfDelay: Infinity });
    this.rl.on('line', (line: string) => {
      try {
        const msg = JSON.parse(line);
        this._dispatch(msg);
      } catch {
        // skip malformed lines
      }
    });

    // Initialize handshake
    const result = await this.request('initialize', {}) as { version: string; session_id: string; model: string; configured?: boolean };
    this._sessionId = result.session_id;
    this._model = result.model;
    // serve is the source of truth for "is a key present" (it may load a .env
    // the editor never saw) — trust it over the editor's own environment.
    if (result.configured) this._hasApiKey = true;
  }

  async dispose(): Promise<void> {
    this._disposing = true; // suppress the auto-reconnect for a deliberate stop
    try {
      await this.request('shutdown', {});
    } catch {
      // ignore
    }
    this.rejectAllPending(new Error('Disposed'));
    this.rl?.close();
    this.process?.kill();
    this.process = null;
    this.rl = null;
  }

  // ── request / notification ───────────────────────────────────────────

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (!this.process || !this.process.stdin || !this.process.stdin.writable) {
      throw new Error('rockycode serve is not connected');
    }
    const id = this.nextId++;
    const msg = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n';

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Request timed out: ${method}`));
      }, 30000);

      this.pending.set(id, { resolve, reject, timer });
      // Guard the write: a serve that died between the check above and here would
      // otherwise raise EPIPE/SIGPIPE instead of a catchable rejection.
      try {
        this.process!.stdin!.write(msg);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    });
  }

  onNotification(method: string, handler: NotificationHandler): void {
    if (!this.handlers.has(method)) {
      this.handlers.set(method, new Set());
    }
    this.handlers.get(method)!.add(handler);
  }

  offNotification(method: string, handler: NotificationHandler): void {
    this.handlers.get(method)?.delete(handler);
  }

  // ── accessors ────────────────────────────────────────────────────────

  get sessionId(): string | null {
    return this._sessionId;
  }

  get model(): string | null {
    return this._model;
  }

  get hasApiKey(): boolean {
    return this._hasApiKey;
  }

  // ── internals ────────────────────────────────────────────────────────

  private _dispatch(msg: Record<string, unknown>): void {
    if (msg.id !== undefined && msg.id !== null) {
      // Response
      const id = msg.id as number;
      const pending = this.pending.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(id);
        if (msg.error) {
          pending.reject(new Error((msg.error as { message: string }).message));
        } else {
          pending.resolve(msg.result);
        }
      }
    } else if (msg.method) {
      // Notification
      const handlers = this.handlers.get(msg.method as string);
      if (handlers) {
        const params = (msg.params || {}) as Record<string, unknown>;
        for (const h of handlers) {
          h(params);
        }
      }
    }
  }

  private rejectAllPending(error: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(error);
    }
    this.pending.clear();
  }
}
