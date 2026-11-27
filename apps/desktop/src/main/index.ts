import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, protocol, shell } from 'electron';
import { join, resolve } from 'node:path';
import {realpathSync} from 'node:fs';
import {writeFile} from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import {PreviewDocuments, artifactCsp} from './artifacts';
import { Runtime } from './runtime';
import {UiPreferences} from './preferences';
import {UpdateReminder} from './updates';
import {DEFAULT_FEED, type UpdateCheck} from '../shared/updates';
import {isLanguagePreference, resolveLocale, translate, type Locale} from '../shared/i18n';
import {colorScheme, isThemePreference, themeArgument, windowBackground} from '../shared/theme';
import {localizeKeyed} from '../shared/diagnostics';
import {nativeMenuTemplate} from '../shared/menu';
import {isPresetId, presetSavePayload} from '../shared/presets';
import type { Snapshot } from '../shared/protocol';

protocol.registerSchemesAsPrivileged([{scheme: 'rocky-artifact', privileges: {standard: true, secure: true}}]);
const previews = new PreviewDocuments();
const fixture = process.env.ROCKYCODE_DESKTOP_FIXTURE === '1';
const testMode = fixture && process.env.ROCKYCODE_DESKTOP_TEST === '1';
// app.isPackaged flips true when the dev binary is rebranded (executable name
// differs from Electron), so dev launchers mark the unpackaged layout explicitly.
const packaged = app.isPackaged && process.env.ROCKYCODE_DESKTOP_DEV !== '1';
if (process.env.ROCKYCODE_DESKTOP_DATA_DIR) app.setPath('userData', resolve(process.env.ROCKYCODE_DESKTOP_DATA_DIR));
else if (fixture) app.setPath('userData', join(app.getPath('appData'), 'RockyCode-fixture'));
else app.setPath('userData', join(app.getPath('appData'), 'RockyCode'));
app.setName('RockyCode');
let window: BrowserWindow | null = null;
let runtime: Runtime;
let preferences: UiPreferences;
let updates: UpdateReminder;
let launchCheck: Promise<UpdateCheck> = Promise.resolve({status: 'skipped'});
// The active UI language; every message this process throws is in it.
let locale: () => Locale = () => 'en';
let exiting = false;
const permittedWorkspaces = new Set<string>();
const rendererPath = join(__dirname, '../renderer/index.html');
const rendererURL = pathToFileURL(rendererPath).href;

function text(value: unknown, max = 100_000): string {
  if (typeof value !== 'string' || value.length > max) throw new Error(translate(locale(), 'diagnostic.invalidArgument'));
  return value;
}
function id(value: unknown) {
  const result = text(value, 64);
  if (!/^[a-f0-9]{32}$/.test(result)) throw new Error(translate(locale(), 'diagnostic.invalidSession'));
  return result;
}
function handle(channel: string, action: (...args: any[]) => any) {
  ipcMain.handle(channel, (event, ...args) => {
    if (!window || event.sender !== window.webContents || event.senderFrame?.url !== rendererURL) throw new Error(translate(locale(), 'diagnostic.forbiddenCaller'));
    return action(...args);
  });
}

// The colour behind the first paint, so a launch never flashes another theme.
function background() {
  return windowBackground(preferences.theme()) ?? (nativeTheme.shouldUseDarkColors ? '#151519' : '#f8f8f6');
}
function startRuntime() {
  runtime = new Runtime({root: app.getAppPath(), resources: process.resourcesPath, packaged, data: app.getPath('userData'), fixture,
    localize: (key, fallback, params) => localizeKeyed(locale(), fallback ?? key, key, params)});
  runtime.on('event', event => { if (window && !window.isDestroyed()) window.webContents.send('desktop:event', event); });
}
app.whenReady().then(() => {
  // A cached dev Electron bundle can still carry electron.icns after a pull.
  // Use the approved source asset without downloading or rewriting that bundle.
  if (process.platform === 'darwin' && !packaged) {
    app.dock?.setIcon(join(app.getAppPath(), 'assets/RockyCode.iconset/icon_512x512@2x.png'));
  }
  preferences = new UiPreferences(app.getPath('userData'), app.getLocale());
  locale = () => { const settings = preferences.language(); return resolveLocale(settings.preference, settings.systemLocale); };
  // A dark theme pins the OS colour scheme, so the stylesheet's dark rules and native controls follow it.
  nativeTheme.themeSource = colorScheme(preferences.theme());
  // The update reminder: one GET of a small version file, at most once a day (src/main/updates.ts).
  // A test feed is honoured only in the unpackaged layout; fixture runs without one never fetch.
  const feedOverride = !packaged ? process.env.ROCKYCODE_DESKTOP_UPDATE_FEED : undefined;
  updates = new UpdateReminder(app.getPath('userData'), app.getVersion(), feedOverride || DEFAULT_FEED);
  if (!fixture || feedOverride) launchCheck = updates.check(false);
  const checkFromMenu = async () => {
    const result = await updates.check(true);
    if (window && !window.isDestroyed()) window.webContents.send('desktop:event', {type: 'update', manual: true, result});
  };
  const updateMenu = () => Menu.setApplicationMenu(Menu.buildFromTemplate(nativeMenuTemplate(locale(), packaged, {checkForUpdates: () => void checkFromMenu()})));
  handle('desktop:update-launch', () => launchCheck);
  handle('desktop:update-settings', () => updates.settings);
  handle('desktop:update-enable', (enabled: unknown) => {
    if (typeof enabled !== 'boolean') throw new Error(translate(locale(), 'diagnostic.invalidArgument'));
    try { return updates.setEnabled(enabled); }
    catch { throw new Error(translate(locale(), 'update.saveFailed')); }
  });
  handle('desktop:update-check', () => updates.check(true));
  handle('desktop:update-skip', (version: unknown) => { updates.skip(text(version, 64)); });
  handle('desktop:update-open', async (url: unknown) => {
    // Only the link the accepted feed named; never an arbitrary renderer URL.
    const target = updates.link(text(url, 2048));
    if (!target) throw new Error(translate(locale(), 'diagnostic.invalidArgument'));
    await shell.openExternal(target);
  });
  handle('desktop:get-language', () => preferences.language());
  handle('desktop:set-language', (preference: unknown) => {
    if (!isLanguagePreference(preference)) throw new Error(translate(locale(), 'error.invalidLanguage'));
    let settings;
    try { settings = preferences.setLanguage(preference); }
    catch { throw new Error(translate(locale(), 'error.saveLanguage')); }
    updateMenu();
    return settings;
  });
  handle('desktop:get-theme', () => preferences.theme());
  handle('desktop:set-theme', (theme: unknown) => {
    if (!isThemePreference(theme)) throw new Error(translate(locale(), 'error.invalidTheme'));
    try { preferences.setTheme(theme); }
    catch { throw new Error(translate(locale(), 'error.saveTheme')); }
    nativeTheme.themeSource = colorScheme(theme);
    window?.setBackgroundColor(background());
    return theme;
  });
  protocol.handle('rocky-artifact', request => {
    const html = previews.get(request.url);
    return new Response(html ?? 'Preview expired', {status: html === undefined ? 404 : 200, headers: {'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': artifactCsp, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}});
  });
  startRuntime();
  handle('desktop:initialize', async () => {
    const snapshot = await runtime.peer.request<Snapshot>('initialize');
    if (snapshot.protocolVersion !== 1) throw new Error(translate(locale(), 'diagnostic.incompatibleProtocol'));
    snapshot.threads.forEach(t => permittedWorkspaces.add(t.workspace));
    return snapshot;
  });
  handle('desktop:choose-workspace', async () => {
    if (testMode && process.env.ROCKYCODE_DESKTOP_TEST_WORKSPACE) {
      const path = realpathSync(process.env.ROCKYCODE_DESKTOP_TEST_WORKSPACE); permittedWorkspaces.add(path); return path;
    }
    const result = await dialog.showOpenDialog(window!, {title: translate(locale(), 'dialog.chooseWorkspace'), properties: ['openDirectory', 'createDirectory']});
    const path = result.canceled ? null : realpathSync(result.filePaths[0]);
    if (path) permittedWorkspaces.add(path);
    return path;
  });
  handle('desktop:create-thread', (workspace: unknown) => {
    const path = realpathSync(text(workspace, 4096));
    if (!permittedWorkspaces.has(path)) throw new Error(translate(locale(), 'diagnostic.chooseWorkspace'));
    return runtime.peer.request('thread/create', {workspace: path});
  });
  handle('desktop:dismiss-task-message', (thread, message) => runtime.peer.request('task/dismiss', {threadId: id(thread), messageId: id(message)}));
  handle('desktop:create-task', (thread, prompt) => runtime.peer.request('task/create', {threadId: id(thread), prompt: text(prompt, 20_000)}));
  handle('desktop:task-message', (thread, target, message) => runtime.peer.request('task/send', {threadId: id(thread), target: id(target), message: text(message, 20_000)}));
  handle('desktop:race-list', cursor => runtime.peer.request('race/list', {cursor: cursor == null ? null : id(cursor)}));
  handle('desktop:race-get', raceId => runtime.peer.request('race/get', {raceId: id(raceId)}));
  handle('desktop:race-start', (input: unknown) => {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error(translate(locale(), 'race.error.invalidRequest'));
    const request = input as Record<string, unknown>;
    if (JSON.stringify(request).length > 30_000) throw new Error(translate(locale(), 'race.error.requestTooLarge'));
    const source = request.workspace == null ? null : realpathSync(text(request.workspace, 4096));
    if (source && !permittedWorkspaces.has(source)) throw new Error(translate(locale(), 'race.error.folderPicker'));
    return runtime.peer.request('race/start', {prompt: text(request.prompt, 20_000), models: request.models, limits: request.limits, workspace: source});
  });
  handle('desktop:race-cancel', raceId => runtime.peer.request('race/cancel', {raceId: id(raceId)}));
  handle('desktop:race-judge', (raceId, model) => runtime.peer.request('race/judge', {raceId: id(raceId), model: text(model, 256)}));
  handle('desktop:race-score', (raceId, scores) => {
    if (JSON.stringify(scores)?.length > 10_000) throw new Error(translate(locale(), 'race.error.scoresTooLarge'));
    return runtime.peer.request('race/score', {raceId: id(raceId), scores});
  });
  handle('desktop:race-preview', raceId => runtime.peer.request('race/preview', {raceId: id(raceId)}));
  handle('desktop:race-export', async (raceId, digest) => {
    await runtime.peer.request<{json: string}>('race/export', {raceId: id(raceId), digest: text(digest, 64)});
    const target = await dialog.showSaveDialog(window!, {title: translate(locale(), 'dialog.exportRace'), defaultPath: 'rocky-race-case.json', filters: [{name: 'JSON', extensions: ['json']}]});
    if (target.canceled || !target.filePath) return {saved: false};
    // Recheck the reviewed digest after the dialog; never save newer unreviewed content.
    const verified = await runtime.peer.request<{json: string}>('race/export', {raceId: id(raceId), digest: text(digest, 64)});
    await writeFile(target.filePath, verified.json, {encoding: 'utf8', mode: 0o600});
    return {saved: true};
  });
  handle('desktop:start-turn', (threadId, prompt, delivery) => {
    if (delivery !== undefined && delivery !== 'queue' && delivery !== 'interrupt') throw new Error(translate(locale(), 'diagnostic.invalidDelivery'));
    return runtime.peer.request('turn/start', {threadId: id(threadId), prompt: text(prompt), delivery: delivery ?? 'reject'});
  });
  handle('desktop:resume-queue', threadId => runtime.peer.request('turn/resume', {threadId: id(threadId)}));
  handle('desktop:remove-queued', (threadId, messageId) => runtime.peer.request('turn/dequeue', {threadId: id(threadId), messageId: id(messageId)}));
  handle('desktop:cancel-turn', threadId => runtime.peer.request('turn/cancel', {threadId: id(threadId)}));
  handle('desktop:approval', (threadId, approvalId, decision) => {
    if (decision !== 'once' && decision !== 'session' && decision !== 'deny') throw new Error(translate(locale(), 'diagnostic.invalidApproval'));
    return runtime.peer.request('approval/respond', {threadId: id(threadId), approvalId: id(approvalId), decision});
  });
  handle('desktop:parked', (threadId, stepId, decision, prompt) => {
    if (decision !== 'run' && decision !== 'skip') throw new Error(translate(locale(), 'diagnostic.invalidApproval'));
    return runtime.peer.request('parked/decide', {threadId: id(threadId), stepId: id(stepId), decision, ...(decision === 'run' ? {prompt: text(prompt)} : {})});
  });
  handle('desktop:set-mode', (threadId, mode) => {
    if (mode !== 'careful' && mode !== 'ask' && mode !== 'yolo') throw new Error(translate(locale(), 'diagnostic.invalidMode'));
    return runtime.peer.request('mode/set', {threadId: id(threadId), mode});
  });
  handle('desktop:rename-thread', (threadId, title) => runtime.peer.request('thread/rename', {threadId: id(threadId), title: text(title, 200)}));
  handle('desktop:delete-thread', threadId => runtime.peer.request('thread/delete', {threadId: id(threadId)}));
  handle('desktop:artifact-preview', async (threadId, reference) => {
    const artifact = await runtime.peer.request<{path: string; title: string; html: string}>('artifact/read', {threadId: id(threadId), path: text(reference, 8192)});
    return {path: artifact.path, title: artifact.title, url: previews.add(artifact.html)};
  });
  handle('desktop:artifact-external', async (threadId, reference) => {
    // Revalidate on each explicit click; never open arbitrary renderer URLs.
    const artifact = await runtime.peer.request<{path: string}>('artifact/read', {threadId: id(threadId), path: text(reference, 8192)});
    await shell.openExternal(pathToFileURL(artifact.path).href);
  });
  handle('desktop:artifact-close', url => { previews.remove(text(url, 200)); });
  handle('desktop:list-models', () => runtime.peer.request('models/list'));
  // Add a provider key from inside the app. The key goes straight to the host, which tests and
  // saves it with the core's saver (the OS keychain when asked and there is one, else the key
  // file); nothing here stores or logs it.
  handle('desktop:key-providers', () => runtime.peer.request('setup/providers'));
  handle('desktop:save-key', (endpoint: unknown, key: unknown, keychain: unknown) => runtime.peer.request('setup/saveKey', {endpoint: text(endpoint, 64), key: text(key, 512), keychain: keychain === true}));
  handle('desktop:set-model', (threadId, model) => runtime.peer.request('model/set', {threadId: id(threadId), model: text(model, 200)}));
  handle('desktop:restart-runtime', async () => {
    await runtime.stop();
    startRuntime();
    const snapshot = await runtime.peer.request<Snapshot>('initialize');
    if (snapshot.protocolVersion !== 1) throw new Error(translate(locale(), 'diagnostic.incompatibleProtocol'));
    snapshot.threads.forEach(t => permittedWorkspaces.add(t.workspace));
    return snapshot;
  });
  handle('desktop:save-note', (threadId, content) => runtime.peer.request('note/save', {threadId: id(threadId), content: text(content, 1_000_000)}));
  handle('desktop:save-draft', (threadId, content) => runtime.peer.request('draft/save', {threadId: id(threadId), content: text(content)}));
  handle('desktop:list-presets', () => runtime.peer.request('preset/list'));
  handle('desktop:save-preset', (input: unknown) => {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('参数无效');
    const request = input as Record<string, unknown>;
    if (Object.keys(request).some(key => key !== 'id' && key !== 'name' && key !== 'body')) throw new Error('预设只保存名称和正文');
    return runtime.peer.request('preset/save', presetSavePayload(request));
  });
  handle('desktop:delete-preset', (presetId: unknown) => {
    if (!isPresetId(presetId)) throw new Error('参数无效');
    return runtime.peer.request('preset/delete', {id: presetId});
  });
  window = new BrowserWindow({width: 1320, height: 880, minWidth: 900, minHeight: 620, title: 'RockyCode', backgroundColor: background(), titleBarStyle: 'hiddenInset', trafficLightPosition: {x: 18, y: 20}, autoHideMenuBar: true, webPreferences: {preload: join(__dirname, '../preload/index.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, additionalArguments: [themeArgument(preferences.theme())]}});
  window.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-frame-navigate', event => {
    if (!previews.get(event.url)) event.preventDefault();
  });
  updateMenu();
  void window.loadFile(rendererPath);
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (exiting || !runtime) return;
  event.preventDefault(); exiting = true;
  void runtime.stop().finally(() => app.quit());
});
