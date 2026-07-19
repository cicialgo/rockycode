/**
 * Status bar item showing Rocky's current state (thinking / working / idle).
 */
import * as vscode from 'vscode';

export class RockyStatusBar implements vscode.Disposable {
  private item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left,
      100,
    );
    this.item.name = 'Rocky Code';
    this.item.tooltip = 'Rocky Code Agent';
    this.item.command = 'rockycode.newSession';
    this.setIdle();
    this.item.show();
  }

  setThinking(): void {
    this.item.text = '$(sync~spin) Rocky thinking...';
    this.item.backgroundColor = undefined;
  }

  setResponding(): void {
    this.item.text = '$(pulse) Rocky responding...';
    this.item.backgroundColor = undefined;
  }

  setWorking(tool?: string): void {
    this.item.text = tool
      ? `$(tools) Rocky: ${tool}...`
      : '$(tools) Rocky working...';
    this.item.backgroundColor = undefined;
  }

  setIdle(): void {
    this.item.text = '$(sparkle) Rocky';
    this.item.backgroundColor = undefined;
  }

  setError(): void {
    this.item.text = '$(error) Rocky error';
    this.item.backgroundColor = new vscode.ThemeColor(
      'statusBarItem.errorBackground',
    );
  }

  setCompacting(): void {
    this.item.text = '$(fold) Rocky compacting...';
    this.item.backgroundColor = undefined;
  }

  dispose(): void {
    this.item.dispose();
  }
}
