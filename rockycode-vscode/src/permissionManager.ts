/**
 * Tool-approval policy + prompt routing.
 *
 * Intercepts session/request_permission notifications, applies the session's
 * auto-decisions and permission mode, and — when a human decision is needed —
 * asks through a *prompter*. The provider wires a prompter that renders the
 * Codex/Claude-Code-style keyboard prompt inside the webview (Enter approves,
 * arrows choose); if no webview is available we fall back to a native dialog.
 */
import * as vscode from 'vscode';
import type { RockyConnection } from './rockyConnection';

/** The three outcomes — mirrors rocky's own TUI (once / session / deny). */
export type PermChoice = 'once' | 'session' | 'deny';

export interface PermPrompt {
  eventId: string;
  tool: string;
  detail: string;
  risk: string;
}

export class PermissionManager {
  private _autoAllow: Set<string> = new Set(); // tools allowed for the rest of this session
  private _prompter: ((req: PermPrompt) => Promise<PermChoice>) | null = null;

  constructor(private connection: RockyConnection) {
    this.connection.onNotification('session/request_permission', this._onPermissionRequest.bind(this));
  }

  /** The provider calls this to route prompts into the webview UI. */
  setPrompter(fn: (req: PermPrompt) => Promise<PermChoice>): void {
    this._prompter = fn;
  }

  /** Reset per-session auto-decisions (on a new session). */
  reset(): void {
    this._autoAllow.clear();
  }

  private async _onPermissionRequest(params: Record<string, unknown>): Promise<void> {
    const eventId = params.event_id as string;
    const tool = params.tool as string;
    const risk = (params.risk as string) || 'risky';
    const args = params.args as Record<string, unknown> | undefined;

    // Session auto-allow short-circuits before any prompt.
    if (this._autoAllow.has(tool)) {
      await this._respond(eventId, true);
      return;
    }

    const permissionMode = vscode.workspace.getConfiguration('rockycode').get<string>('permissionMode', 'ask');

    if (permissionMode === 'yolo') {
      await this._respond(eventId, true);
      return;
    }
    // 'careful' prompts on everything; 'ask' only on the 'risky' tier.
    if (permissionMode === 'ask' && risk !== 'risky') {
      await this._respond(eventId, true);
      return;
    }

    // Readable description of the call. You must be able to review the WHOLE
    // command before approving it — cap only to guard against a pathological
    // multi-KB arg (e.g. a big write_file body); real commands show in full.
    const CAP = 4000;
    const argsSummary = args
      ? Object.entries(args)
          .map(([k, v]) => {
            const s = String(v);
            return `${k}=${s.length > CAP ? s.slice(0, CAP) + ' …[truncated]' : s}`;
          })
          .join(', ')
      : '';
    const detail = `${tool}(${argsSummary})`;

    const prompt: PermPrompt = { eventId, tool, detail, risk };
    const choice = this._prompter
      ? await this._prompter(prompt)
      : await this._nativePrompt(prompt);

    if (choice === 'session') {
      this._autoAllow.add(tool);
    }
    await this._respond(eventId, choice !== 'deny');
  }

  /** Fallback when no webview is available to host the prompt. */
  private async _nativePrompt(req: PermPrompt): Promise<PermChoice> {
    const pick = await vscode.window.showWarningMessage(
      `Rocky wants to run: ${req.detail}`,
      { modal: false },
      { title: 'Run Once', id: 'once' as const },
      { title: 'Allow for Session', id: 'session' as const },
      { title: 'Deny', id: 'deny' as const },
    );
    return pick?.id ?? 'deny';
  }

  private async _respond(eventId: string, allowed: boolean): Promise<void> {
    try {
      await this.connection.request('session/permission_response', {
        event_id: eventId,
        allowed,
      });
    } catch {
      // If the request fails, the server will timeout and deny.
    }
  }
}
