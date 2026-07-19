/**
 * Gathers editor context before each chat request:
 * active file, cursor position, selected text, open tabs, lint errors.
 */
import * as vscode from 'vscode';

export interface EditorContext {
  activeFile?: string;
  cursorLine?: number;
  cursorColumn?: number;
  selection?: string;
  selectionLines?: { start: number; end: number };
  openTabs: string[];
  lintErrors: string[];
}

/** A snippet the user explicitly pinned to the chat (file + line range + code). */
export interface SelectionContext {
  file: string;        // workspace-relative path
  startLine: number;
  endLine: number;
  code: string;
  lang: string;
  symbol?: string;     // enclosing symbol, e.g. "function embed_query" (Phase 2)
}

function relPath(uri: vscode.Uri): string {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  return root && uri.fsPath.startsWith(root) ? uri.fsPath.slice(root.length + 1) : uri.fsPath;
}

/** Deepest document symbol whose range contains pos, as "kind Name" (qualified). */
function enclosingSymbol(
  symbols: vscode.DocumentSymbol[], pos: vscode.Position, prefix = '',
): string | undefined {
  for (const s of symbols) {
    if (s.range.contains(pos)) {
      const here = `${vscode.SymbolKind[s.kind].toLowerCase()} ${prefix}${s.name}`;
      const deeper = s.children?.length ? enclosingSymbol(s.children, pos, `${s.name}.`) : undefined;
      return deeper || here;
    }
  }
  return undefined;
}

/**
 * Structured context for the active editor's selection (or the whole file when
 * nothing is selected). Phase 2: best-effort enclosing symbol via the language
 * server, so rocky is told "this is inside function X" without having to guess.
 */
export async function gatherSelectionContext(): Promise<SelectionContext | null> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return null;
  const doc = editor.document;
  const sel = editor.selection;
  const whole = sel.isEmpty;

  const ctx: SelectionContext = {
    file: relPath(doc.uri),
    startLine: (whole ? 0 : sel.start.line) + 1,
    endLine: (whole ? doc.lineCount - 1 : sel.end.line) + 1,
    code: doc.getText(whole ? undefined : sel),
    lang: doc.languageId,
  };

  const enrich = vscode.workspace.getConfiguration('rockycode').get<boolean>('enrichSelectionContext', true);
  if (enrich && !whole) {
    try {
      const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
        'vscode.executeDocumentSymbolProvider', doc.uri,
      );
      if (symbols?.length) {
        ctx.symbol = enclosingSymbol(symbols, sel.start);
      }
    } catch {
      /* no symbol provider for this language — fine, the pointer alone is useful */
    }
  }
  return ctx;
}

export function gatherEditorContext(): EditorContext {
  const editor = vscode.window.activeTextEditor;
  const ctx: EditorContext = { openTabs: [], lintErrors: [] };

  if (!editor) {
    return ctx;
  }

  const doc = editor.document;
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

  // Active file (relative path)
  if (workspaceRoot && doc.uri.fsPath.startsWith(workspaceRoot)) {
    ctx.activeFile = doc.uri.fsPath.slice(workspaceRoot.length + 1);
  } else {
    ctx.activeFile = doc.uri.fsPath;
  }

  // Cursor position
  ctx.cursorLine = editor.selection.active.line + 1;
  ctx.cursorColumn = editor.selection.active.character + 1;

  // Selected text
  if (!editor.selection.isEmpty) {
    ctx.selection = doc.getText(editor.selection);
    ctx.selectionLines = {
      start: editor.selection.start.line + 1,
      end: editor.selection.end.line + 1,
    };
  }

  // Open tabs
  ctx.openTabs = vscode.window.tabGroups.all
    .flatMap(g => g.tabs)
    .map(t => {
      const input = t.input as { uri?: vscode.Uri };
      if (input?.uri) {
        if (workspaceRoot && input.uri.fsPath.startsWith(workspaceRoot)) {
          return input.uri.fsPath.slice(workspaceRoot.length + 1);
        }
        return input.uri.fsPath;
      }
      return t.label;
    })
    .filter(Boolean);

  return ctx;
}

export async function gatherLintErrors(): Promise<string[]> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return [];
  }

  const doc = editor.document;
  const diagnostics = vscode.languages.getDiagnostics(doc.uri);

  return diagnostics
    .filter(d => d.severity <= vscode.DiagnosticSeverity.Warning)
    .slice(0, 10)
    .map(d => {
      const line = d.range.start.line + 1;
      const col = d.range.start.character + 1;
      const sev = d.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning';
      return `${doc.fileName}:${line}:${col}: ${sev}: ${d.message}`;
    });
}

export function formatContextForPrompt(ctx: EditorContext, lintErrors: string[]): string {
  const parts: string[] = [];

  parts.push('[editor context]');

  if (ctx.activeFile) {
    let fileInfo = `active file: ${ctx.activeFile}`;
    if (ctx.cursorLine) {
      fileInfo += ` (line ${ctx.cursorLine}`;
      if (ctx.cursorColumn) {
        fileInfo += `, col ${ctx.cursorColumn}`;
      }
      fileInfo += ')';
    }
    parts.push(fileInfo);
  }

  if (ctx.selection && ctx.selectionLines) {
    parts.push(`selection (lines ${ctx.selectionLines.start}-${ctx.selectionLines.end}):`);
    parts.push('```');
    parts.push(ctx.selection);
    parts.push('```');
  }

  if (ctx.openTabs.length > 0) {
    parts.push(`open tabs: ${ctx.openTabs.slice(0, 10).join(', ')}`);
  }

  if (lintErrors.length > 0) {
    parts.push('lint issues:');
    for (const err of lintErrors) {
      parts.push(`  ${err}`);
    }
  }

  return parts.join('\n');
}
