/**
 * Manages inline diff display for edit_file tool results.
 * Maps old_string/new_string edits to VS Code's diff editor.
 */
import * as vscode from 'vscode';

export class DiffManager {
  private _lastDiffs: Map<string, vscode.Uri> = new Map();

  /**
   * Apply an edit_file result as a VS Code workspace edit, then optionally
   * show a diff view for the user to review.
   */
  async applyEdit(
    filePath: string,
    oldString: string,
    newString: string,
  ): Promise<void> {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!workspaceRoot) {
      return;
    }

    const uri = vscode.Uri.joinPath(workspaceRoot, filePath);

    let doc: vscode.TextDocument | undefined;
    try {
      doc = await vscode.workspace.openTextDocument(uri);
    } catch {
      // File doesn't exist yet — create it
      const edit = new vscode.WorkspaceEdit();
      edit.createFile(uri, { overwrite: true });
      edit.insert(uri, new vscode.Position(0, 0), newString);
      await vscode.workspace.applyEdit(edit);
      return;
    }

    if (!doc) {
      return;
    }

    const fullText = doc.getText();
    const idx = fullText.indexOf(oldString);

    if (idx >= 0) {
      const startPos = doc.positionAt(idx);
      const endPos = doc.positionAt(idx + oldString.length);

      const edit = new vscode.WorkspaceEdit();
      edit.replace(uri, new vscode.Range(startPos, endPos), newString);
      await vscode.workspace.applyEdit(edit);

      // Show the diff in a side-by-side view
      const tempUri = uri.with({ scheme: 'rockycode-diff', query: `original-${Date.now()}` });
      this._lastDiffs.set(filePath, tempUri);

      // Reopen the document to see changes applied
      await vscode.window.showTextDocument(uri, { preview: false });
    }
  }

  /**
   * Show a diff between two versions of a file using VS Code's diff editor.
   */
  async showDiff(filePath: string, original: string, modified: string): Promise<void> {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!workspaceRoot) {
      return;
    }

    const originalUri = vscode.Uri.parse(`untitled:${filePath}.original`);
    const modifiedUri = vscode.Uri.parse(`untitled:${filePath}.modified`);

    // Create temp documents
    const origDoc = await vscode.workspace.openTextDocument(originalUri);
    const origEdit = new vscode.WorkspaceEdit();
    origEdit.insert(originalUri, new vscode.Position(0, 0), original);
    await vscode.workspace.applyEdit(origEdit);

    const modDoc = await vscode.workspace.openTextDocument(modifiedUri);
    const modEdit = new vscode.WorkspaceEdit();
    modEdit.insert(modifiedUri, new vscode.Position(0, 0), modified);
    await vscode.workspace.applyEdit(modEdit);

    await vscode.commands.executeCommand('vscode.diff', originalUri, modifiedUri, `${filePath} (changes)`);
  }

  handleToolFinished(tool: string, output: string): boolean {
    // Return true if we handled it as a diff
    if (tool !== 'edit_file' && tool !== 'write_file') {
      return false;
    }
    // Diffs are applied automatically at the engine level; this is a hook
    // for future inline diff annotations.
    return true;
  }
}
