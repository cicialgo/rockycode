/**
 * rockycode VS Code extension — Cline-style webview chat panel.
 */
import * as vscode from 'vscode';
import { RockyConnection } from './rockyConnection';
import { RockyChatViewProvider } from './rockyProvider';
import { PermissionManager } from './permissionManager';
import { DiffManager } from './diffManager';
import { RockyStatusBar } from './statusBar';
import { gatherSelectionContext } from './editorContext';

const output = vscode.window.createOutputChannel('Rocky Code', { log: true });

let connection: RockyConnection | undefined;
let statusBar: RockyStatusBar | undefined;
let chatProvider: RockyChatViewProvider | undefined;
let reconnectAttempts = 0;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output.info('Rocky Code activating...');

  // One-time migration: move any plaintext apiKey setting into encrypted Secret
  // Storage, then clear it so the key stops living in (syncable) settings.json.
  const legacyKey = vscode.workspace.getConfiguration('rockycode').get<string>('apiKey', '');
  if (legacyKey) {
    await context.secrets.store('rockycode.apiKey', legacyKey);
    await vscode.workspace.getConfiguration('rockycode').update('apiKey', undefined, vscode.ConfigurationTarget.Global);
    output.info('Migrated the plaintext rockycode.apiKey setting into encrypted Secret Storage.');
  }

  connection = new RockyConnection(context.secrets);
  statusBar = new RockyStatusBar();

  // Self-heal: if serve dies while the extension host lives (a stray kill, a
  // crash), reconnect instead of sitting dead. Capped so a serve that can't
  // start doesn't loop forever; the counter resets on a healthy connect.
  connection.onUnexpectedExit(() => {
    const wf = vscode.workspace.workspaceFolders?.[0];
    if (!wf) return;
    if (reconnectAttempts >= 3) {
      output.error('rockycode serve keeps exiting — stopping auto-reconnect.');
      statusBar?.setError();
      vscode.window.showErrorMessage('Rocky Code: serve keeps exiting. See the Rocky Code output channel.');
      return;
    }
    reconnectAttempts++;
    output.warn(`serve exited unexpectedly — reconnecting (attempt ${reconnectAttempts}/3)...`);
    setTimeout(() => {
      const w = vscode.workspace.workspaceFolders?.[0];
      if (w) void tryConnect(w.uri.fsPath);
    }, 800);
  });

  const permissions = new PermissionManager(connection);
  const diffManager = new DiffManager();
  chatProvider = new RockyChatViewProvider(
    context.extensionUri, connection, statusBar, permissions, diffManager, context.secrets,
  );

  // Register the UI + commands FIRST, unconditionally — the extension must stay
  // usable even if the serve backend fails to start. Otherwise a bad
  // pythonPath / missing folder aborted activation and every command reported
  // "not found". Connection errors surface in the panel; the user fixes settings
  // and it reconnects.
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('rockycode.chatView', chatProvider, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand('rockycode.openChat', () => {
      vscode.commands.executeCommand('workbench.view.extension.rockycode-sidebar');
    }),
    vscode.commands.registerCommand('rockycode.newSession', async () => {
      if (!connection) return;
      try {
        const result = await connection.request('initialize', {}) as { session_id: string };
        vscode.window.showInformationMessage(`New session: ${result.session_id.slice(0, 8)}...`);
      } catch (err) {
        vscode.window.showErrorMessage(`Rocky Code: ${err instanceof Error ? err.message : String(err)}`);
      }
    }),
    vscode.commands.registerCommand('rockycode.cancelTurn', async () => {
      await connection?.request('cancel', { session_id: connection.sessionId }).catch(() => {});
      statusBar?.setIdle();
    }),
    vscode.commands.registerCommand('rockycode.addToChat', async () => {
      if (!chatProvider) return;
      const ctx = await gatherSelectionContext();
      if (!ctx || !ctx.code.trim()) return;
      chatProvider.addContext(ctx);
      // Bring the panel forward so the pinned chip is visible.
      vscode.commands.executeCommand('workbench.view.extension.rockycode-sidebar');
    }),
    connection,
    statusBar,
    { dispose: () => permissions?.reset() },
  );
  output.info('Webview provider + commands registered');

  // Auto-restart serve when a key setting changes (pythonPath included, so
  // fixing the CLI path reconnects without a reload).
  const restartKeys = ['pythonPath', 'model', 'apiKey', 'baseUrl', 'thinking', 'reasoningEffort', 'maxTokens', 'contextWindow', 'systemPrompt'];
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(async (e) => {
      if (restartKeys.some((k) => e.affectsConfiguration(`rockycode.${k}`))) {
        output.info('Config changed — restarting rockycode serve...');
        try {
          await connection?.dispose();
          const wf = vscode.workspace.workspaceFolders?.[0];
          if (wf && connection) {
            await connection.start(wf.uri.fsPath);
            chatProvider?._postMessage?.({ type: 'configSaved', model: connection.model });
          }
        } catch (err) {
          output.error(`Restart failed: ${err}`);
          vscode.window.showErrorMessage(`Rocky Code: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }),
    // Connect (or reconnect) when the workspace folder appears — e.g. the user
    // opened a folder after the extension already activated folderless.
    vscode.workspace.onDidChangeWorkspaceFolders(async () => {
      const wf = vscode.workspace.workspaceFolders?.[0];
      if (wf && connection && !connection.sessionId) {
        await tryConnect(wf.uri.fsPath);
      }
    }),
  );

  // Live context pill: keep the webview showing the active file + selection, so
  // the user always sees what rocky gets — no keybinding to remember.
  const pushCtx = () => chatProvider?.pushActiveContext();
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(pushCtx),
    vscode.window.onDidChangeTextEditorSelection(pushCtx),
  );

  // Now attempt the connection — NON-fatal. Needs a folder for the workdir.
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  if (!workspaceFolder) {
    output.warn('No workspace folder open — open a project folder to start Rocky.');
    vscode.window.showInformationMessage('Rocky Code: open a project folder to start Rocky.');
    statusBar.setError();
  } else {
    await tryConnect(workspaceFolder.uri.fsPath);
  }

  output.info('Rocky Code ready');
}

/** Start the serve connection; report failures without aborting the extension. */
async function tryConnect(workdir: string): Promise<void> {
  if (!connection) return;
  try {
    statusBar?.setThinking();
    output.info(`Starting rockycode serve (workdir: ${workdir})...`);
    await connection.start(workdir);
    statusBar?.setIdle();
    reconnectAttempts = 0; // healthy connection — clear the auto-reconnect budget
    output.info(`Connected. session=${connection.sessionId} model=${connection.model} hasApiKey=${connection.hasApiKey}`);
    // Drive the setup card off serve's real state, not the editor's env guess.
    if (connection.hasApiKey) {
      chatProvider?._postMessage?.({ type: 'configSaved', model: connection.model });
    } else {
      output.warn('No API key configured — paste one in the panel (stored encrypted) or use ~/.rockycode/.env.');
      chatProvider?._postMessage?.({ type: 'noApiKey' });
    }
    chatProvider?._postMessage?.({ type: 'keyHint', hint: connection.keyHint });
  } catch (err) {
    statusBar?.setError();
    const msg = err instanceof Error ? err.message : String(err);
    output.error(`Failed to start rockycode serve: ${msg}`);
    vscode.window.showErrorMessage(`Rocky Code: ${msg}`);
  }
}

export function deactivate(): void {
  output.info('Rocky Code deactivating...');
  connection?.dispose();
}
