import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopAPI, RuntimeEvent } from '../shared/protocol';
import {applyTheme, themeFromArguments} from '../shared/theme';

// Put the saved theme on <html> before the stylesheet paints: the main process passes it on
// argv, and the root element does not exist yet when a preload starts.
const launchTheme = themeFromArguments(process.argv);
if (launchTheme !== 'system') {
  if (document.documentElement) applyTheme(document.documentElement, launchTheme);
  else new MutationObserver((_records, observer) => {
    if (!document.documentElement) return;
    applyTheme(document.documentElement, launchTheme);
    observer.disconnect();
  }).observe(document, {childList: true});
}
const api: DesktopAPI = {
  getLanguage: () => ipcRenderer.invoke('desktop:get-language'),
  setLanguage: preference => ipcRenderer.invoke('desktop:set-language', preference),
  getTheme: () => ipcRenderer.invoke('desktop:get-theme'),
  setTheme: theme => ipcRenderer.invoke('desktop:set-theme', theme),
  launchUpdateCheck: () => ipcRenderer.invoke('desktop:update-launch'),
  getUpdateSettings: () => ipcRenderer.invoke('desktop:update-settings'),
  setUpdateChecks: enabled => ipcRenderer.invoke('desktop:update-enable', enabled),
  checkForUpdate: () => ipcRenderer.invoke('desktop:update-check'),
  skipUpdate: version => ipcRenderer.invoke('desktop:update-skip', version),
  openUpdate: url => ipcRenderer.invoke('desktop:update-open', url),
  listRaces: cursor => ipcRenderer.invoke('desktop:race-list', cursor),
  getRace: id => ipcRenderer.invoke('desktop:race-get', id),
  startRace: input => ipcRenderer.invoke('desktop:race-start', input),
  cancelRace: id => ipcRenderer.invoke('desktop:race-cancel', id),
  judgeRace: (id, model) => ipcRenderer.invoke('desktop:race-judge', id, model),
  scoreRace: (id, scores) => ipcRenderer.invoke('desktop:race-score', id, scores),
  previewRace: id => ipcRenderer.invoke('desktop:race-preview', id),
  exportRace: (id, digest) => ipcRenderer.invoke('desktop:race-export', id, digest),
  initialize: () => ipcRenderer.invoke('desktop:initialize'),
  chooseWorkspace: () => ipcRenderer.invoke('desktop:choose-workspace'),
  dismissTaskMessage: (thread, message) => ipcRenderer.invoke('desktop:dismiss-task-message', thread, message),
  createTask: (thread, prompt) => ipcRenderer.invoke('desktop:create-task', thread, prompt),
  sendTaskMessage: (thread, target, message) => ipcRenderer.invoke('desktop:task-message', thread, target, message),
  createThread: workspace => ipcRenderer.invoke('desktop:create-thread', workspace),
  startTurn: (thread, prompt, delivery) => ipcRenderer.invoke('desktop:start-turn', thread, prompt, delivery),
  resumeQueue: thread => ipcRenderer.invoke('desktop:resume-queue', thread),
  removeQueued: (thread, message) => ipcRenderer.invoke('desktop:remove-queued', thread, message),
  cancelTurn: thread => ipcRenderer.invoke('desktop:cancel-turn', thread),
  respondApproval: (thread, approval, decision) => ipcRenderer.invoke('desktop:approval', thread, approval, decision),
  decideParked: (thread, step, decision, prompt) => ipcRenderer.invoke('desktop:parked', thread, step, decision, prompt),
  setPermissionMode: (thread, mode) => ipcRenderer.invoke('desktop:set-mode', thread, mode),
  renameThread: (thread, title) => ipcRenderer.invoke('desktop:rename-thread', thread, title),
  deleteThread: thread => ipcRenderer.invoke('desktop:delete-thread', thread),
  restartRuntime: () => ipcRenderer.invoke('desktop:restart-runtime'),
  previewArtifact: (thread, path) => ipcRenderer.invoke('desktop:artifact-preview', thread, path),
  openArtifactExternal: (thread, path) => ipcRenderer.invoke('desktop:artifact-external', thread, path),
  closeArtifactPreview: url => ipcRenderer.invoke('desktop:artifact-close', url),
  listModels: () => ipcRenderer.invoke('desktop:list-models'),
  keyProviders: () => ipcRenderer.invoke('desktop:key-providers'),
  saveKey: (endpoint, key, keychain) => ipcRenderer.invoke('desktop:save-key', endpoint, key, keychain),
  setModel: (thread, model) => ipcRenderer.invoke('desktop:set-model', thread, model),
  saveDraft: (thread, content) => ipcRenderer.invoke('desktop:save-draft', thread, content),
  saveNote: (thread, content) => ipcRenderer.invoke('desktop:save-note', thread, content),
  listPresets: () => ipcRenderer.invoke('desktop:list-presets'),
  savePreset: input => ipcRenderer.invoke('desktop:save-preset', input),
  deletePreset: id => ipcRenderer.invoke('desktop:delete-preset', id),
  onEvent: listener => {
    const callback = (_event: unknown, data: RuntimeEvent) => listener(data);
    ipcRenderer.on('desktop:event', callback);
    return () => ipcRenderer.removeListener('desktop:event', callback);
  },
};
contextBridge.exposeInMainWorld('rocky', api);
