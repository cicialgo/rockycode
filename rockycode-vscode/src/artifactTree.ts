import * as vscode from 'vscode';
import type { RockyConnection, RockySessionResult } from './rockyConnection';
import type { ArtifactInfo } from './protocol';
export type { ArtifactInfo } from './protocol';

interface ArtifactStatus {
  session_id: string;
  server_running: boolean;
  server_url: string | null;
  artifacts: ArtifactInfo[];
}

interface SessionArtifacts {
  artifacts: Map<string, ArtifactInfo>;
  serverRunning: boolean;
  serverUrl: string | null;
}

class ArtifactSessionItem extends vscode.TreeItem {
  readonly kind = 'session';

  constructor(
    readonly sessionId: string,
    readonly state: SessionArtifacts,
    active: boolean,
  ) {
    super(`Session ${sessionId.slice(0, 8)}`, vscode.TreeItemCollapsibleState.Expanded);
    const n = state.artifacts.size;
    const server = state.serverRunning ? 'server on' : 'server off';
    this.description = `${active ? 'active · ' : ''}${n} artifact${n === 1 ? '' : 's'} · ${server}`;
    this.contextValue = 'rockyArtifactSession';
    this.iconPath = new vscode.ThemeIcon(active ? 'radio-tower' : 'history');
    this.tooltip = state.serverUrl
      ? `Rocky session ${sessionId}\n${state.serverUrl}`
      : `Rocky session ${sessionId}\nArtifact server stopped`;
  }
}

class ArtifactItem extends vscode.TreeItem {
  readonly kind = 'artifact';

  constructor(readonly info: ArtifactInfo, serverRunning: boolean) {
    super(info.title, vscode.TreeItemCollapsibleState.None);
    const connected = info.open_clients > 0;
    const state = connected ? `${info.open_clients} open` : 'saved';
    this.description = `${info.live ? 'live' : 'static'} · ${state}`;
    this.contextValue = 'rockyArtifact';
    this.iconPath = new vscode.ThemeIcon(
      connected ? 'circle-filled' : 'circle-outline',
      connected ? new vscode.ThemeColor('testing.iconPassed') : undefined,
    );
    this.tooltip = `${info.path}\n${info.url}`;
    this.command = {
      command: 'rockycode.openArtifact',
      title: 'Open Artifact',
      arguments: [info, serverRunning],
    };
  }
}

type ArtifactNode = ArtifactSessionItem | ArtifactItem;

/** Session-grouped Artifact inventory driven by rockycode serve notifications. */
export class ArtifactTreeProvider implements vscode.TreeDataProvider<ArtifactNode>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<ArtifactNode | undefined | null | void>();
  readonly onDidChangeTreeData = this.changed.event;
  private readonly sessions = new Map<string, SessionArtifacts>();
  private activeSession: string | null = null;
  private readonly notificationHandler: (params: Record<string, unknown>) => void;
  private readonly sessionHandler: (session: RockySessionResult) => void;

  constructor(private readonly connection: RockyConnection) {
    this.notificationHandler = (params) => this.onArtifactChanged(params);
    this.sessionHandler = (session) => this.setActiveSession(session.session_id);
    connection.onNotification('session/artifact_changed', this.notificationHandler);
    connection.onSessionChanged(this.sessionHandler);
  }

  dispose(): void {
    this.connection.offNotification('session/artifact_changed', this.notificationHandler);
    this.connection.offSessionChanged(this.sessionHandler);
    this.changed.dispose();
  }

  reset(sessionId: string | null): void {
    this.sessions.clear();
    this.activeSession = sessionId;
    if (sessionId) this.ensureSession(sessionId);
    this.changed.fire();
  }

  setActiveSession(sessionId: string): void {
    this.activeSession = sessionId;
    this.ensureSession(sessionId);
    this.changed.fire();
  }

  async refreshActive(): Promise<void> {
    const sid = this.activeSession || this.connection.sessionId;
    if (!sid) return;
    const result = await this.connection.request('artifact/list', {
      session_id: sid,
    }) as ArtifactStatus;
    this.applyStatus(result);
  }

  async stopActiveServer(): Promise<boolean> {
    const sid = this.activeSession || this.connection.sessionId;
    if (!sid) return false;
    const result = await this.connection.request('artifact/stop', {
      session_id: sid,
    }) as ArtifactStatus & { stopped: boolean };
    this.applyStatus(result);
    return result.stopped;
  }

  getTreeItem(element: ArtifactNode): vscode.TreeItem {
    return element;
  }

  getChildren(element?: ArtifactNode): ArtifactNode[] {
    if (!element) {
      return [...this.sessions.entries()]
        .sort(([a], [b]) => {
          if (a === this.activeSession) return -1;
          if (b === this.activeSession) return 1;
          return a.localeCompare(b);
        })
        .map(([sid, state]) => new ArtifactSessionItem(
          sid, state, sid === this.activeSession,
        ));
    }
    if (element.kind === 'session') {
      return [...element.state.artifacts.values()]
        .sort((a, b) => b.updated_at - a.updated_at)
        .map((info) => new ArtifactItem(info, element.state.serverRunning));
    }
    return [];
  }

  private ensureSession(sessionId: string): SessionArtifacts {
    let state = this.sessions.get(sessionId);
    if (!state) {
      state = { artifacts: new Map(), serverRunning: false, serverUrl: null };
      this.sessions.set(sessionId, state);
    }
    return state;
  }

  private applyStatus(status: ArtifactStatus): void {
    const state = this.ensureSession(status.session_id);
    state.serverRunning = status.server_running;
    state.serverUrl = status.server_url;
    state.artifacts = new Map(status.artifacts.map((item) => [item.name, item]));
    this.changed.fire();
  }

  private onArtifactChanged(params: Record<string, unknown>): void {
    const sid = params.session_id as string;
    const artifact = params.artifact as ArtifactInfo | undefined;
    if (!sid || !artifact) return;
    const state = this.ensureSession(sid);
    state.serverRunning = Boolean(params.server_running);
    state.serverUrl = (params.server_url as string | null) || null;
    state.artifacts.set(artifact.name, artifact);
    this.changed.fire();
  }
}
