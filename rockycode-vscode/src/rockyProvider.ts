/**
 * WebviewViewProvider — Cline-style chat panel.
 *
 * Manages the webview chat UI and bridges between the webview (user actions)
 * and the rockycode serve process (via RockyConnection).
 */
import * as vscode from 'vscode';
import * as path from 'path';
import type { RockyConnection } from './rockyConnection';
import type { PermissionManager, PermChoice, PermPrompt } from './permissionManager';
import type { RockyStatusBar } from './statusBar';
import type { DiffManager } from './diffManager';
import { gatherEditorContext, gatherLintErrors, formatContextForPrompt, gatherSelectionContext } from './editorContext';
import type { SelectionContext } from './editorContext';

export class RockyChatViewProvider implements vscode.WebviewViewProvider {
  private _view: vscode.WebviewView | null = null;
  private _pendingPerms = new Map<string, (choice: PermChoice) => void>();
  private _lastEditor?: vscode.TextEditor;
  private _lastCtxKey = '';

  constructor(
    private readonly _extensionUri: vscode.Uri,
    private connection: RockyConnection,
    private statusBar: RockyStatusBar,
    private permissions: PermissionManager,
    private diff: DiffManager,
    private secrets: vscode.SecretStorage,
  ) {}

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this._view = webviewView;

    // Route tool-approval prompts into this webview (Enter approves, arrows
    // choose — mirroring rocky's own TUI).
    this.permissions.setPrompter((req) => this._promptPermission(req));

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this._extensionUri, 'src', 'webview'),
      ],
    };

    webviewView.webview.html = this._getHtml(webviewView.webview);

    // Show the webview immediately (retain it when hidden)
    webviewView.show?.(true);

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage(
      async (msg) => {
        switch (msg.type) {
          case 'sendMessage':
            await this._handleChat(msg.text);
            break;
          case 'cancelTurn':
            await this.connection.request('cancel', {
              session_id: this.connection.sessionId,
            });
            this.statusBar.setIdle();
            this._postMessage({ type: 'stateChanged', state: 'idle' });
            break;
          case 'saveConfig':
            await this._saveConfig(msg.apiKey, msg.baseUrl, msg.model);
            break;
          case 'openSettings':
            vscode.commands.executeCommand(
              'workbench.action.openSettings', 'rockycode',
            );
            break;
          case 'newSession':
            await this.connection.request('initialize', {});
            this._postMessage({ type: 'clearChat' });
            break;
          case 'permissionResponse': {
            const cb = this._pendingPerms.get(msg.eventId);
            if (cb) cb((msg.choice as PermChoice) || 'deny');
            break;
          }
          case 'pinContext': {
            // Clicking the context pill pins the current selection as a chip —
            // a discoverable path, no keybinding needed.
            const sel = await gatherSelectionContext();
            if (sel && sel.code.trim()) this.addContext(sel);
            break;
          }
        }
      },
    );

    // Check API key and show setup if needed
    if (!(this.connection as any).hasApiKey) {
      this._postMessage({ type: 'noApiKey' });
    }

    // Update session info in the webview header
    this._postMessage({
      type: 'stateChanged',
      state: 'idle',
      sessionId: this.connection.sessionId,
      model: this.connection.model,
    });

    // Prime the live context pill for the current editor.
    this._lastCtxKey = '';
    this.pushActiveContext();
  }

  // ── permission prompt ────────────────────────────────────────────────

  /**
   * Ask the human to approve one tool call inside the webview. Resolves with
   * their choice; times out to 'deny' just under the server's own permission
   * timeout, so a closed/ignored panel fails closed rather than hanging.
   */
  private _promptPermission(req: PermPrompt): Promise<PermChoice> {
    return new Promise<PermChoice>((resolve) => {
      const view = this._view;
      if (!view) {
        resolve('deny'); // no webview to ask through; server would time out to deny anyway
        return;
      }
      view.show?.(true); // bring the panel forward so the prompt is visible
      let timer: ReturnType<typeof setTimeout>;
      const done = (choice: PermChoice) => {
        clearTimeout(timer);
        this._pendingPerms.delete(req.eventId);
        resolve(choice);
      };
      timer = setTimeout(() => done('deny'), 110_000);
      this._pendingPerms.set(req.eventId, done);
      this._postMessage({
        type: 'permissionRequest',
        eventId: req.eventId,
        tool: req.tool,
        detail: req.detail,
        risk: req.risk,
      });
    });
  }

  // ── chat handling ────────────────────────────────────────────────────

  private async _handleChat(userText: string): Promise<void> {
    const autoInject = vscode.workspace.getConfiguration('rockycode').get<boolean>('autoInjectContext', true);
    let message = userText;

    if (autoInject) {
      const ctx = gatherEditorContext();
      const lintErrors = await gatherLintErrors();
      const contextBlock = formatContextForPrompt(ctx, lintErrors);
      if (contextBlock) {
        message = `${contextBlock}\n\n${message}`;
      }
    }

    this.statusBar.setThinking();

    // Register notification handlers for this turn.
    // Cleanup happens when turn_finished or error is received, NOT on a timer.
    const cleanup = this._wireNotifications();

    try {
      await this.connection.request('chat', {
        session_id: this.connection.sessionId,
        message,
      });
    } catch (err) {
      this._postMessage({
        type: 'error',
        message: err instanceof Error ? err.message : String(err),
      });
      this.statusBar.setError();
      cleanup();
    }
    // NOTE: do NOT call cleanup() here — it's called by the
    // turn_finished / error notification handlers in _wireNotifications.
  }

  private _wireNotifications(): () => void {
    // Define cleanup first so handlers can reference it
    let cleaned = false;
    const cleanup = () => {
      if (cleaned) return;
      cleaned = true;
      this.connection.offNotification('session/state_changed', stateHandler);
      this.connection.offNotification('session/thinking_delta', thinkingHandler);
      this.connection.offNotification('session/text_delta', textHandler);
      this.connection.offNotification('session/tool_started', toolStartHandler);
      this.connection.offNotification('session/tool_finished', toolFinishHandler);
      this.connection.offNotification('session/compacted', compactedHandler);
      this.connection.offNotification('session/turn_finished', turnFinishedHandler);
      this.connection.offNotification('session/error', errorHandler);
    };

    const stateHandler = (params: Record<string, unknown>) => {
      const state = params.state as string;
      this._postMessage({ type: 'stateChanged', state });
      switch (state) {
        case 'thinking': this.statusBar.setThinking(); break;
        case 'responding': this.statusBar.setResponding(); break;
        case 'tool': this.statusBar.setWorking(); break;
        case 'compacting': this.statusBar.setCompacting(); break;
        case 'idle': this.statusBar.setIdle(); break;
        case 'error': this.statusBar.setError(); break;
      }
    };

    const thinkingHandler = (params: Record<string, unknown>) => {
      this._postMessage({ type: 'thinkingDelta', text: params.text });
    };

    const textHandler = (params: Record<string, unknown>) => {
      this._postMessage({ type: 'textDelta', text: params.text });
    };

    const toolStartHandler = (params: Record<string, unknown>) => {
      this._postMessage({
        type: 'toolStarted',
        callId: params.call_id,
        tool: params.tool,
        args: params.args,
      });
    };

    const toolFinishHandler = (params: Record<string, unknown>) => {
      this._postMessage({
        type: 'toolFinished',
        callId: params.call_id,
        tool: params.tool,
        output: params.output,
        ok: params.ok,
        duration: params.duration_s,
      });

      if (params.tool === 'edit_file' || params.tool === 'write_file') {
        this.diff.handleToolFinished(params.tool as string, params.output as string);
      }

      // Open artifacts inside VS Code
      if (params.tool === 'create_artifact' && params.ok) {
        const out = params.output as string;
        const match = out.match(/url:\s*(https?:\/\/[^\s]+)/);
        if (match) {
          vscode.env.openExternal(vscode.Uri.parse(match[1]));
        } else {
          // Static artifact: extract file path and open in VS Code
          const fileMatch = out.match(/opened in browser:\s*(file:\/\/[^\s]+)/);
          if (fileMatch) {
            vscode.commands.executeCommand('simpleBrowser.show', fileMatch[1]);
          }
        }
      }
    };

    const compactedHandler = (params: Record<string, unknown>) => {
      this._postMessage({
        type: 'compacted',
        strategy: params.strategy,
        tokensBefore: params.tokens_before,
        tokensAfter: params.tokens_after,
      });
    };

    const turnFinishedHandler = (params: Record<string, unknown>) => {
      this._postMessage({
        type: 'turnFinished',
        steps: params.steps,
        usage: params.usage,
      });
      cleanup();
    };

    const errorHandler = (params: Record<string, unknown>) => {
      this._postMessage({ type: 'error', message: params.message });
      cleanup();
    };

    // Permission requests are handled by PermissionManager (native VS Code dialogs),
    // not forwarded to the webview.

    this.connection.onNotification('session/state_changed', stateHandler);
    this.connection.onNotification('session/thinking_delta', thinkingHandler);
    this.connection.onNotification('session/text_delta', textHandler);
    this.connection.onNotification('session/tool_started', toolStartHandler);
    this.connection.onNotification('session/tool_finished', toolFinishHandler);
    this.connection.onNotification('session/compacted', compactedHandler);
    this.connection.onNotification('session/turn_finished', turnFinishedHandler);
    this.connection.onNotification('session/error', errorHandler);

    return cleanup;
  }

  // ── helpers ──────────────────────────────────────────────────────────

  _postMessage(msg: Record<string, unknown>): void {
    this._view?.webview.postMessage(msg);
  }

  private _getHtml(webview: vscode.Webview): string {
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri, 'media', 'chat.html',
    );
    // Load marked.js for proper markdown rendering (bundled in media/)
    const markedPath = vscode.Uri.joinPath(
      this._extensionUri, 'media', 'marked.js',
    );
    let rawHtml: string;
    try {
      rawHtml = require('fs').readFileSync(htmlPath.fsPath, 'utf-8');
    } catch {
      rawHtml = '<html><body><h1>Failed to load chat UI</h1></body></html>';
    }
    let markedJs = '';
    try {
      markedJs = require('fs').readFileSync(markedPath.fsPath, 'utf-8');
    } catch {
      // marked not available, fall back to basic rendering
    }
    // Syntax highlighter (esbuild-bundled to media/highlight.js). Optional —
    // if it's missing, code blocks just render unhighlighted.
    let hljsJs = '';
    try {
      const hljsPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'highlight.js');
      hljsJs = require('fs').readFileSync(hljsPath.fsPath, 'utf-8');
    } catch {
      // no highlighter built — fine
    }

    const nonce = _getNonce();

    // Global replace: {{nonce}} appears in both the CSP meta and the inline
    // <script>, {{cspSource}} in three CSP directives — a first-match .replace()
    // would leave the later ones as literal placeholders (breaking the CSP and
    // the script nonce). Replace the MARKED_JS marker last (it's a single spot).
    return rawHtml
      .replace(/\{\{nonce\}\}/g, nonce)
      .replace(/\{\{cspSource\}\}/g, webview.cspSource)
      .replace('<!-- MARKED_JS -->', '<script nonce="' + nonce + '">' + markedJs + '</script>')
      .replace('<!-- HLJS_JS -->', hljsJs ? '<script nonce="' + nonce + '">' + hljsJs + '</script>' : '');
  }

  private async _saveConfig(apiKey: string, baseUrl: string, model: string): Promise<void> {
    const cfg = vscode.workspace.getConfiguration('rockycode');
    // The key goes into encrypted Secret Storage, NOT the plaintext (syncable)
    // settings. baseUrl/model aren't secret, so they stay in settings.
    if (apiKey) await this.secrets.store('rockycode.apiKey', apiKey);
    await cfg.update('baseUrl', baseUrl, vscode.ConfigurationTarget.Global);
    await cfg.update('model', model, vscode.ConfigurationTarget.Global);
    // Restart the connection with new config
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (workspaceFolder) {
      await this.connection.dispose();
      await this.connection.start(workspaceFolder.uri.fsPath);
    }
    this._postMessage({ type: 'configSaved', model });
    this._postMessage({ type: 'keyHint', hint: this.connection.keyHint });
  }

  /** Public method to programmatically add context to the chat input. */
  addContext(ctx: SelectionContext): void {
    this._postMessage({ type: 'addContext', ctx });
  }

  /**
   * Show the active file (and selection lines) as a live pill in the input, so
   * the user always SEES what auto-inject will attach — no shortcut to remember.
   * Keeps the last real file editor when focus moves to the webview/terminal
   * (which null out activeTextEditor).
   */
  pushActiveContext(): void {
    const active = vscode.window.activeTextEditor;
    if (active && active.document.uri.scheme === 'file') this._lastEditor = active;
    const editor = active ?? this._lastEditor;
    const auto = vscode.workspace.getConfiguration('rockycode').get<boolean>('autoInjectContext', true);
    let ctx: { file: string; startLine?: number; endLine?: number } | null = null;
    if (auto && editor && editor.document.uri.scheme === 'file') {
      const doc = editor.document;
      const sel = editor.selection;
      const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const file = root && doc.uri.fsPath.startsWith(root) ? doc.uri.fsPath.slice(root.length + 1) : doc.uri.fsPath;
      ctx = sel.isEmpty ? { file } : { file, startLine: sel.start.line + 1, endLine: sel.end.line + 1 };
    }
    const key = JSON.stringify(ctx);
    if (key === this._lastCtxKey) return;   // dedupe cursor-move spam
    this._lastCtxKey = key;
    this._postMessage({ type: 'activeContext', ctx });
  }
}

function _getNonce(): string {
  let text = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}
