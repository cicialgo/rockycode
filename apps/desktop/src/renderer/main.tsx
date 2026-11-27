import React, { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Markdown, {defaultUrlTransform} from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowDown, ArrowUpRight, ArrowUp, Check, ChevronRight, CircleHelp, CirclePause, FileText, Flag, FolderOpen, LoaderCircle, MessageSquare, PanelRightClose, Pencil, Plus, Square, Trash2, X } from 'lucide-react';
import { isActive, type ArtifactPreview, type Approval, type ApprovalDecision, type DeliveryMode, type Message, type ParkedStep, type ModelChoice, type PermissionMode, type Thread, type RuntimeInfo, type KeyProvider } from '../shared/protocol';
import { useThread, useThreads, workspace } from './store';
import { presetStore } from './preset-store';
import { PresetButton, PresetPanel } from './PresetPanel';
import './styles.css';
import {I18nProvider, useI18n} from './i18n';
import {isLanguagePreference, locales, translate, type StaticTranslationKey} from '../shared/i18n';
import {localizeKeyed, localizeCapabilityDetail} from '../shared/diagnostics';
import { TaskConnections } from './TaskConnections';
import { RacePanel } from './RacePanel';
import { ThemePicker } from './ThemePicker';
import type {UpdateCheck, UpdateFeed} from '../shared/updates';

const basename = (path: string) => path.split(/[/\\]/).filter(Boolean).pop() || path;
// An untitled thread (the host stores '' since 1.0.0; '新任务' is the pre-1.0.0 stored default).
const defaultTitle = (thread: Thread) => thread.messages.length === 0 && (!thread.title || thread.title === '新任务');
const cleanError = (error: string) => error.replace(/^Error invoking remote method '[^']+': Error: /, '');

// Decorative mark; Vite bundles the canonical brand SVG through its CSS mask.
function ProductIcon({className = ''}: {className?: string}) {
  return <span className={`product-icon ${className}`} aria-hidden="true"/>;
}

function App() {
  const {t, locale, preference, loading: languageLoading, error: languageError, dismissError: dismissLanguageError, setLanguage} = useI18n();
  const threads = useThreads();
  const [view, setView] = useState<'chat' | 'race'>('chat');
  const [active, setActive] = useState<string | null>(null);
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [error, setError] = useState('');
  const [noteOpen, setNoteOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [connected, setConnected] = useState(true);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // The update reminder: a banner for a newer version, a persisted off switch, a manual check.
  const [update, setUpdate] = useState<UpdateFeed | null>(null);
  const [updateChecks, setUpdateChecks] = useState<boolean | null>(null);
  const [updateStatus, setUpdateStatus] = useState<'' | 'checking' | 'upToDate' | 'failed'>('');
  const applyUpdate = useCallback((result: UpdateCheck, manual: boolean) => {
    if (result.status === 'available') { setUpdate(result.update); setUpdateStatus(''); }
    else if (manual) setUpdateStatus(result.status === 'upToDate' ? 'upToDate' : result.status === 'failed' ? 'failed' : '');
  }, []);
  const activeRef = useRef(active); activeRef.current = active;
  const report = useCallback((error: unknown) => setError(error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error)), []);
  useEffect(() => {
    const unlisten = window.rocky.onEvent(event => {
      if (event.type === 'disconnected') { setConnected(false); setError(event.error); }
      else if (event.type === 'update') applyUpdate(event.result, event.manual);
      else workspace.accept(event);
    });
    void window.rocky.initialize().then(snapshot => {
      workspace.hydrate(snapshot); presetStore.hydrate(snapshot.presets); setRuntime(snapshot.runtime);
      const saved = localStorage.getItem('active-thread');
      setActive(snapshot.threads.some(t => t.id === saved) ? saved : snapshot.threads[0]?.id ?? null);
    }).catch(report);
    void window.rocky.getUpdateSettings().then(settings => setUpdateChecks(settings.enabled)).catch(report);
    void window.rocky.launchUpdateCheck().then(result => applyUpdate(result, false)).catch(() => undefined);
    return unlisten;
  }, [report, applyUpdate]);
  const checkNow = useCallback(async () => {
    setUpdateStatus('checking');
    try { applyUpdate(await window.rocky.checkForUpdate(), true); } catch { setUpdateStatus('failed'); }
  }, [applyUpdate]);
  const toggleUpdateChecks = useCallback(async (enabled: boolean) => {
    // Optimistic: the box flips at once; a failed save puts it back and says so.
    setUpdateChecks(enabled);
    try { setUpdateChecks((await window.rocky.setUpdateChecks(enabled)).enabled); }
    catch (e) { setUpdateChecks(!enabled); report(e); }
  }, [report]);
  const later = useCallback(async () => {
    const skipped = update;
    setUpdate(null);
    if (skipped) { try { await window.rocky.skipUpdate(skipped.version); } catch (e) { report(e); } }
  }, [update, report]);
  useEffect(() => { if (active) localStorage.setItem('active-thread', active); }, [active]);
  const create = useCallback(async (choose = false) => {
    setCreating(true);
    try {
      const current = activeRef.current ? workspace.get(activeRef.current) : null;
      const root = choose || !current ? await window.rocky.chooseWorkspace() : current.workspace;
      if (!root) return;
      const thread = await window.rocky.createThread(root);
      workspace.accept({type: 'thread', threadId: thread.id, runId: null, thread}); setActive(thread.id); setView('chat');
    } catch (e) { report(e); } finally { setCreating(false); }
  }, [report]);
  const reconnect = useCallback(async () => {
    try {
      const snapshot = await window.rocky.restartRuntime();
      workspace.hydrate(snapshot); presetStore.hydrate(snapshot.presets); setRuntime(snapshot.runtime); setConnected(true); setError('');
      const current = activeRef.current;
      setActive(snapshot.threads.some(t => t.id === current) ? current : snapshot.threads[0]?.id ?? null);
    } catch (e) { report(e); }
  }, [report]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.isComposing) return;
      if (event.key.toLowerCase() === 'n') { event.preventDefault(); void create(); }
      if (event.key.toLowerCase() === 'j') { event.preventDefault(); setNoteOpen(value => !value); }
    };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, [create]);
  const rename = useCallback(async (threadId: string, title: string) => {
    setRenaming(null);
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      await window.rocky.renameThread(threadId, trimmed);
      workspace.patch(threadId, {title: trimmed});
    } catch (e) { report(e); }
  }, [report]);
  const remove = useCallback(async (threadId: string) => {
    setConfirmDelete(null);
    try {
      await window.rocky.deleteThread(threadId);
      workspace.remove(threadId);
      if (activeRef.current === threadId) {
        const next = workspace.getIndex().find(t => t.id !== threadId);
        setActive(next?.id ?? null);
      }
    } catch (e) { report(e); }
  }, [report]);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="window-drag" />
      <div className="brand"><span className="brand-mark"><ProductIcon/></span><strong>RockyCode</strong><span className="preview-label">{t('preview')}</span></div>
      <button className="new-task" onClick={() => void create()} disabled={creating || !connected || !runtime}><Plus size={17}/>{t('newTask')}<span>⌘ N</span></button>
      <button data-testid="race-nav" className={`race-nav ${view === 'race' ? 'selected' : ''}`} onClick={() => setView('race')}><Flag size={16}/><span>{t('race.nav')}</span></button>
      <div className="sidebar-heading">{t('recentTasks')}<span>{threads.length || ''}</span></div>
      <nav className="thread-list" aria-label={t('taskList')}>
        {threads.length === 0 && <p className="sidebar-empty">{t('sidebarEmpty')}</p>}
        {threads.map(thread => <div key={thread.id} className={`thread-item ${active === thread.id && view === 'chat' ? 'selected' : ''}`}>
          <button className={`thread-button ${active === thread.id && view === 'chat' ? 'selected' : ''}`} data-testid="chat-view-nav" onClick={() => {setActive(thread.id); setView('chat');}}>
            <MessageSquare size={15}/><span><strong>{defaultTitle(thread) ? t('newTask') : thread.title}</strong><small>{basename(thread.workspace)}</small></span>{isActive(thread.status) && <span className="activity-dot"/>}
          </button>
          {renaming === thread.id
            ? <input className="thread-rename" defaultValue={thread.title} autoFocus aria-label={t('thread.renameLabel')} onKeyDown={e => { if (e.nativeEvent.isComposing) return; if (e.key === 'Enter') void rename(thread.id, e.currentTarget.value); if (e.key === 'Escape') setRenaming(null); }} onBlur={e => { if (e.target.value.trim()) void rename(thread.id, e.target.value); else setRenaming(null); }}/>
            : !isActive(thread.status) && <div className="thread-actions">
              <button aria-label={t('thread.renameLabel')} title={t('thread.renameLabel')} onClick={() => { setRenaming(thread.id); setConfirmDelete(null); }}><Pencil size={13}/></button>
              {confirmDelete === thread.id
                ? <button className="confirm-delete" title={t('thread.confirmDelete')} onClick={() => void remove(thread.id)}>{t('thread.confirmDelete')}</button>
                : <button aria-label={t('thread.deleteLabel')} title={t('thread.deleteLabel')} onClick={() => { setConfirmDelete(thread.id); setTimeout(() => setConfirmDelete(current => current === thread.id ? null : current), 3000); }}><Trash2 size={13}/></button>}
            </div>}
        </div>)}
      </nav>
      <button className="workspace-picker" onClick={() => void create(true)} disabled={!runtime || creating}><FolderOpen size={16}/>{t('openProject')}<Plus size={13}/></button>
      <label className="language-picker"><span>{t('language.label')}</span><select aria-label={t('language.label')} title={t('language.help')} value={preference} disabled={languageLoading} onChange={event => { const value = event.target.value; if (isLanguagePreference(value)) void setLanguage(value); }}><option value="system">{t('language.system')}</option>{locales.map(code => <option key={code} value={code}>{translate(code, 'language.name')}</option>)}</select></label>
      <ThemePicker t={t} report={report}/>
      <div className="update-settings"><label className="update-toggle" title={t('update.help')}><input type="checkbox" checked={updateChecks ?? false} disabled={updateChecks === null} onChange={event => void toggleUpdateChecks(event.target.checked)}/><span>{t('update.checkOnLaunch')}</span></label><button className="update-check" onClick={() => void checkNow()} disabled={updateStatus === 'checking'}>{updateStatus === 'checking' ? t('update.checking') : t('update.checkNow')}</button>{(updateStatus === 'upToDate' || updateStatus === 'failed') && <span className="update-status" role="status">{t(updateStatus === 'upToDate' ? 'update.upToDate' : 'update.failed')}</span>}</div>
      <button className="runtime-pill" onClick={() => setInfoOpen(true)}><span className={`status-dot ${runtime?.available && connected ? 'online' : ''}`}/><span>{runtime?.mode === 'fixture' ? t('runtime.fixture') : connected ? t('runtime.local') : t('runtime.disconnected')}</span><CircleHelp size={14}/></button>
    </aside>
    <main className="main-area">
      <header className="topbar window-drag"><div className="breadcrumb"><span>{t('workbench')}</span><ChevronRight size={13}/><strong>{view === 'race' ? t('race.nav') : active ? basename(workspace.get(active)?.workspace || '') : t('start')}</strong></div><button className={`toolbar-button ${noteOpen ? 'active' : ''}`} onClick={() => setNoteOpen(value => !value)} aria-label={t('toggleNotes')} disabled={!active || view === 'race'}><FileText size={16}/>{t('notes')}</button></header>
      {(error || languageError) && <div className="error-banner" role="alert"><span>{cleanError(error || languageError)}</span>{!connected && <button className="reconnect-button" onClick={() => void reconnect()}>{t('error.reconnect')}</button>}<button aria-label={t('dismissError')} onClick={() => { setError(''); dismissLanguageError(); }}><X size={15}/></button></div>}
      {runtime?.mode === 'fixture' && <div className="fixture-banner">{t('fixtureBanner')}</div>}
      {update && <div className="update-banner" role="status" data-testid="update-banner"><span>{t('update.available', {version: update.version})}{update.notes && <small> · {update.notes}</small>}</span><button className="update-open" onClick={() => { if (update) void window.rocky.openUpdate(update.url).catch(report); }}>{t('update.open')}</button><button onClick={() => void later()}>{t('update.later')}</button></div>}
      <div className="chat-surface" hidden={view !== 'chat'}>
      {active ? <ThreadWorkspace key={active} id={active} openTask={setActive} runtime={runtime} connected={connected} checkRuntime={reconnect} noteOpen={noteOpen} closeNote={() => setNoteOpen(false)} report={report}/> : <section className="welcome">
        <div className="welcome-symbol"><ProductIcon/></div><p className="eyebrow">ROCKYCODE WORKSPACE</p><h1>{t('welcome.title')}</h1><p className="welcome-description">{t('welcome.description')}</p>
        {runtime && connected && !runtime.available && <SetupCard runtime={runtime} check={reconnect}/>}
        <button className="primary-button" onClick={() => void create(true)} disabled={creating || !runtime || !connected}><FolderOpen size={17}/>{creating ? t('opening') : t('chooseWorkspace')}</button>
        <div className="welcome-notes"><span><FileText size={15}/>{t('welcome.write')}</span><span><MessageSquare size={15}/>{t('welcome.chat')}</span><span><Square size={13}/>{t('welcome.stop')}</span></div>
        {!runtime && !error && <p className="muted loading"><LoaderCircle size={14} className="spin"/>{t('connecting')}</p>}
      </section>}
      </div>
      <RacePanel hidden={view !== 'race'} connected={connected}/>
      <footer className="app-footer"><span>{t('localFiles')}</span><span>{(runtime?.mode === 'fixture' ? t('runtime.fixture') : (active ? workspace.get(active)?.model : runtime?.model)) || t('noModel')}</span></footer>
    </main>
    {infoOpen && <div className="modal-backdrop" onClick={() => setInfoOpen(false)}><section className="modal" role="dialog" aria-modal="true" aria-label={t('runtime.title')} onClick={e => e.stopPropagation()}><button className="modal-close" onClick={() => setInfoOpen(false)} aria-label={t('runtime.close')}><X size={18}/></button><h2>{t('runtime.title')}</h2><p>{runtime?.mode === 'fixture' ? t('runtime.fixtureDetail') : t('runtime.core')} · {runtime?.version || t('runtime.notConnected')}</p><p className="muted">{(runtime?.mode === 'fixture' ? t('runtime.fixture') : runtime?.model) || t('runtime.noModel')}</p>{runtime?.error && <p className="inline-error">{localizeKeyed(locale, runtime.error, runtime.error_key)}</p>}<h3>{t('runtime.stage')}</h3><ul>{runtime?.limitations.map(item => <li key={item}>{localizeKeyed(locale, item, item)}</li>)}</ul>{runtime?.mode === 'live' && connected && <><h3>{t('setup.add')}</h3><KeyForm saved={reconnect}/></>}{active && <RuntimeCapabilities id={active}/>}</section></div>}
  </div>;
}

// Add a provider key without leaving the app: pick where the model comes from, paste the key.
// The host tests it with one free request and saves it with the core's saver. The field is a
// password input and is emptied the moment the key is sent; nothing in the renderer keeps it.
function KeyForm({saved}: {saved: () => Promise<void>}) {
  const {t} = useI18n();
  const [providers, setProviders] = useState<KeyProvider[] | null>(null);
  const [endpoint, setEndpoint] = useState('');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{text: string; bad: boolean} | null>(null);
  // Where there is a system keychain it is offered, and ticked: the safer home comes first.
  const [keychain, setKeychain] = useState(false);
  const [inKeychain, setInKeychain] = useState(true);
  const load = useCallback(() => window.rocky.keyProviders().then(result => { setProviders(result.providers); setKeychain(result.keychain); setEndpoint(current => current || result.providers[0]?.id || ''); }).catch(() => setProviders([])), []);
  useEffect(() => { void load(); }, [load]);
  if (!providers?.length) return null;
  const place = (provider: KeyProvider) => provider.place === 'keychain' ? t('setup.inKeychain') : provider.place === 'file' ? t('setup.inFile') : t('setup.hasKey');
  const save = async () => {
    const value = key.trim();
    if (!endpoint || !value || busy) return;
    setBusy(true); setKey(''); setStatus({text: t('setup.testing'), bad: false});
    try {
      const result = await window.rocky.saveKey(endpoint, value, keychain && inKeychain);
      const where = t(result.place === 'keychain' ? 'setup.savedKeychain' : 'setup.saved');
      setStatus({text: result.verified ? where : `${where} ${t('setup.unreached')}`, bad: false});
      await load();
      await saved();
    } catch (error) { setStatus({text: cleanError(error instanceof Error ? error.message : String(error)), bad: true}); }
    finally { setBusy(false); }
  };
  return <form className="key-form" onSubmit={event => { event.preventDefault(); void save(); }}>
    <label><span>{t('setup.pick')}</span><select value={endpoint} disabled={busy} onChange={event => setEndpoint(event.target.value)}>{providers.map(provider => <option key={provider.id} value={provider.id}>{provider.label}{provider.configured ? ` · ${place(provider)}` : ''}</option>)}</select></label>
    <label><span>{t('setup.keyLabel')}</span><input type="password" autoComplete="off" spellCheck={false} value={key} disabled={busy} placeholder={t('setup.keyPlaceholder')} onChange={event => setKey(event.target.value)}/></label>
    {keychain && <label className="key-form-check"><input type="checkbox" checked={inKeychain} disabled={busy} onChange={event => setInKeychain(event.target.checked)}/><span>{t('setup.keychain')}</span></label>}
    <div><button type="submit" className="approve-button" disabled={busy || !key.trim() || !endpoint}>{busy ? t('setup.testing') : t('setup.save')}</button></div>
    {status && <p className={status.bad ? 'approval-warning' : ''} role="status">{status.text}</p>}
  </form>;
}

// Nothing can run yet: add a key right here, or run the first-run setup in a terminal and check
// again without relaunching. (The reason used to sit only behind the runtime "?".)
function SetupCard({runtime, check}: {runtime: RuntimeInfo; check: () => Promise<void>}) {
  const {t, locale} = useI18n();
  const [copied, setCopied] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState(false);
  const command = runtime.setup_command || 'rockycode';
  const missingCore = runtime.error_key === 'diagnostic.missingRuntime';
  const title = missingCore ? t('diagnostic.runtimeUnavailable') : t('setup.title');
  return <section className="approval setup-card" role="status" aria-label={title}>
    <strong>{title}</strong>
    <p>{missingCore ? localizeKeyed(locale, runtime.error || '', runtime.error_key) : t('setup.body')}</p>
    {!missingCore && !!runtime.keychain_silent?.length && <p className="approval-warning">{t('setup.keychainSilent')}</p>}
    {!missingCore && <KeyForm saved={check}/>}
    {!missingCore && <p>{t('setup.terminal')}</p>}
    {!missingCore && <pre>{command}</pre>}
    {checked && !checking && <p className="approval-warning">{t('setup.still')}</p>}
    <div>{!missingCore && <button onClick={() => { void navigator.clipboard.writeText(command).then(() => setCopied(true), () => setCopied(false)); }}>{copied ? t('setup.copied') : t('setup.copy')}</button>}
      <button disabled={checking} onClick={() => { setChecking(true); void check().finally(() => { setChecking(false); setChecked(true); }); }}>{checking ? t('setup.checking') : t('setup.check')}</button></div>
  </section>;
}

function ModelPicker({id, disabled, report}: {id: string; disabled: boolean; report: (error: unknown) => void}) {
  const {t} = useI18n();
  const thread = useThread(id)!;
  const [state, setState] = useState<{model: string; models: ModelChoice[]} | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { void window.rocky.listModels().then(setState).catch(report); }, [report]);
  const model = thread.model ?? state?.model ?? '';
  const selected = state?.models.find(m => m.id === model || m.id.endsWith(`:${model}`))?.id ?? '';
  return <label className="task-model-picker"><span>{t('task.model')}</span>
    <select aria-label={t('task.model')} title={t('task.modelHint')} value={selected} disabled={disabled || saving || !state?.models.length} onChange={event => {
      const next = event.target.value; if (!next) return;
      setSaving(true);
      void window.rocky.setModel(id, next).then(result => workspace.patch(id, {model: result.model})).catch(report).finally(() => setSaving(false));
    }}>
      {selected === '' && <option value="">{model || t('noModel')}</option>}
      {state?.models.map(m => <option key={m.id} value={m.id} disabled={!m.configured}>{m.label}{m.vision ? ' · 👁' : ''}{m.configured ? '' : ` · ${t('task.needsKey')}`}</option>)}
    </select><small>{t('task.scope')}</small></label>;
}

function RuntimeCapabilities({id}: {id: string}) {
  const {t, locale} = useI18n();
  const thread = useThread(id);
  const names: Record<string, StaticTranslationKey> = {core: 'capability.core', skills: 'capability.skills', memory: 'capability.memory', memory_index: 'capability.memory_index', web: 'capability.web', mcp: 'capability.mcp', lsp: 'capability.lsp', artifacts: 'capability.artifacts', goal_review: 'capability.goal_review', explore: 'capability.explore', loops: 'capability.loops'};
  const states: Record<string, StaticTranslationKey> = {ready: 'capabilityState.ready', registered: 'capabilityState.registered', pending: 'capabilityState.pending', partial: 'capabilityState.partial', failed: 'capabilityState.failed', disabled: 'capabilityState.disabled', not_configured: 'capabilityState.not_configured'};
  return <div className="runtime-capabilities"><p className="muted">{t('runtime.record')}</p>{thread?.capabilities?.map(cap => <details key={cap.name}><summary>{Object.hasOwn(names, cap.name) ? t(names[cap.name]) : cap.name} · {Object.hasOwn(states, cap.status) ? t(states[cap.status]) : cap.status}</summary><p>{cap.detail ? localizeCapabilityDetail(locale, cap) : t('runtime.noDetail')}</p></details>)}<details><summary>{t('runtime.tools', {count: thread?.tools.length || 0})}</summary><p className="muted">{thread?.tools.join(locale === 'zh' ? '、' : ', ') || t('runtime.afterRun')}</p></details></div>;
}

function ThreadWorkspace({id, runtime, connected, checkRuntime, noteOpen, closeNote, report, openTask}: {openTask: (id: string) => void; id: string; runtime: RuntimeInfo | null; connected: boolean; checkRuntime: () => Promise<void>; noteOpen: boolean; closeNote: () => void; report: (error: unknown) => void}) {
  const {t} = useI18n();
  const thread = useThread(id)!;
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const previewRequest = useRef(0);
  const showArtifact = useCallback(async (path: string) => {
    const request = ++previewRequest.current;
    try {
      const result = await window.rocky.previewArtifact(id, path);
      if (request !== previewRequest.current) { void window.rocky.closeArtifactPreview(result.url).catch(report); return; }
      closeNote(); setPreview(result);
    } catch (error) { if (request === previewRequest.current) report(error); }
  }, [id, closeNote, report]);
  useEffect(() => () => { previewRequest.current++; }, [id]);
  useEffect(() => () => { if (preview) void window.rocky.closeArtifactPreview(preview.url).catch(report); }, [preview, report]);
  const latestArtifact = thread.artifacts?.at(-1)?.path;
  const showArtifactRef = useRef(showArtifact); showArtifactRef.current = showArtifact;
  useEffect(() => { if (latestArtifact) void showArtifactRef.current(latestArtifact); }, [latestArtifact]);
  useEffect(() => { if (noteOpen) { previewRequest.current++; setPreview(null); } }, [noteOpen]);
  const busy = isActive(thread.status);
  const respond = useCallback((decision: ApprovalDecision) => { void window.rocky.respondApproval(id, thread.approval!.id, decision).catch(report); }, [id, report, thread.approval]);
  // Approval keyboard: Enter allow once · S allow for session · Esc deny.
  // Skipped while typing (composer/inputs/IME) so queued typing is never eaten.
  // A call that carries a warning is allowed by its button only: no key allows it.
  useEffect(() => {
    if (!thread.approval) return;
    const key = (event: KeyboardEvent) => {
      if (document.querySelector('.preset-panel, .modal-backdrop')) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.isContentEditable)) return;
      if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
      // A focused control keeps its own Enter: on "Deny" it denies. It used to allow.
      const warned = !!thread.approval?.warning;
      if (event.key === 'Enter') { if (warned || target?.closest('button, a, summary, [role="button"]')) return; event.preventDefault(); respond('once'); }
      else if (event.key === 'Escape') { event.preventDefault(); respond('deny'); }
      else if ((event.key === 's' || event.key === 'S') && thread.approval?.sessionLabel && !warned) { event.preventDefault(); respond('session'); }
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, [thread.approval, respond]);
  return <div className="workspace-panels"><section className="conversation">
    <div className="conversation-heading"><h2>{defaultTitle(thread) ? t('newBeginning') : thread.title}</h2><span className={`task-status ${busy ? 'busy' : ''}`}>{busy && <LoaderCircle size={12} className="spin"/>}{t(`status.${thread.status}`)}</span></div>
    {!!thread.artifacts?.length && <nav className="artifact-list" aria-label={t('artifact.list')}>{thread.artifacts.map(artifact => <button key={artifact.path} onClick={() => void showArtifact(artifact.path)}><FileText size={13}/>{artifact.title}</button>)}</nav>}
    <TaskConnections id={id} connected={connected} openTask={openTask} report={report}/>
    <Transcript thread={thread} preview={path => void showArtifact(path)}/>
    {thread.approval && <ApprovalCard key={thread.approval.id} approval={thread.approval} respond={respond}/>}
    {!!thread.parked?.length && <ParkedList id={id} steps={thread.parked} report={report}/>}
    {runtime && connected && !runtime.available && <SetupCard runtime={runtime} check={checkRuntime}/>}
    <Composer id={id} busy={busy} cancelling={thread.status === 'cancelling'} available={!!runtime?.available && connected} modelsKey={`${runtime?.available}|${runtime?.model}`} report={report}/>
  </section>{preview && <aside className="artifact-panel" aria-label={t('artifact.preview')}><header><strong>{preview.title}</strong><div><button aria-label={t('artifact.reload')} title={t('artifact.reload')} onClick={() => void showArtifact(preview.path)}>↻</button><button aria-label={t('artifact.external')} title={t('artifact.external')} onClick={() => void window.rocky.openArtifactExternal(id, preview.path).catch(report)}><ArrowUpRight size={17}/></button><button aria-label={t('artifact.close')} title={t('artifact.close')} onClick={() => { previewRequest.current++; setPreview(null); }}><X size={17}/></button></div></header><p>{t('artifact.hint')}</p><iframe title={t('artifact.preview')} src={preview.url} sandbox="" referrerPolicy="no-referrer"/></aside>}{noteOpen && <NoteEditor key={id} id={id} close={closeNote} report={report}/>}</div>;
}

// Steps only the user can allow, which a yolo turn refused instead of waiting. Buttons only:
// no key answers them, so a key typed for something else cannot run one.
function ParkedList({id, steps, report}: {id: string; steps: ParkedStep[]; report: (error: unknown) => void}) {
  const {t, locale} = useI18n();
  const decide = (step: ParkedStep, decision: 'run' | 'skip') => void window.rocky.decideParked(id, step.id, decision, decision === 'run' ? t('parked.pickup', {step: step.detail}) : undefined).catch(report);
  return <section className="approval parked" aria-label={t('parked.label')}><strong>{t('parked.title')}</strong><p>{t('parked.hint')}</p>{steps.map(step => <article key={step.id} data-parked={step.id}>
    <p className="parked-step">{step.tool} · {t('parked.asked', {time: new Date(step.at).toLocaleTimeString(locale, {hour: '2-digit', minute: '2-digit'})})}</p>
    {step.warning && <p className="approval-warning">⚠ {step.warning}</p>}<pre>{step.detail}</pre>
    <div><button onClick={() => decide(step, 'skip')}>{t('parked.skip')}</button><button className="approve-button" onClick={() => decide(step, 'run')}>{t('parked.run')}</button></div>
  </article>)}</section>;
}

// key=approval.id remounts per request so the pulse animation replays.
function ApprovalCard({approval, respond}: {approval: Approval; respond: (decision: ApprovalDecision) => void}) {
  const {t} = useI18n();
  const [remaining, setRemaining] = useState(() => approval.expiresAt ? Math.max(0, Math.ceil((approval.expiresAt - Date.now()) / 1000)) : null);
  useEffect(() => {
    if (!approval.expiresAt) return;
    const timer = setInterval(() => setRemaining(Math.max(0, Math.ceil((approval.expiresAt! - Date.now()) / 1000))), 1000);
    return () => clearInterval(timer);
  }, [approval.expiresAt]);
  if (approval.tool === 'runtime.start_mcp' || approval.tool === 'runtime.start_lsp') return <ConnectionApproval approval={approval} respond={respond} remaining={remaining}/>;
  return <section className="approval" key={approval.id} aria-label={t('approval.label')}><strong>{t('approval.title')}</strong><p>{approval.tool}</p>{approval.warning && <p className="approval-warning">⚠ {approval.warning}</p>}<pre>{JSON.stringify(approval.args, null, 2)}</pre><div><button onClick={() => respond('deny')}>{t('approval.deny')}</button>{approval.sessionLabel && <button className="session-button" onClick={() => respond('session')}>{t('approval.session', {label: approval.sessionLabel})}</button>}<button className="approve-button" onClick={() => respond('once')}>{t('approval.allow')}</button></div><p className="approval-keys">{t(approval.warning ? 'approval.keysWarned' : 'approval.keys')}</p>{remaining !== null && <p className="approval-timeout">⏳ {t('approval.timeoutHint', {seconds: remaining})}</p>}</section>;
}

function ConnectionApproval({approval, respond, remaining}: {approval: Approval; respond: (decision: ApprovalDecision) => void; remaining: number | null}) {
  const {t} = useI18n();
  const mcp = approval.tool === 'runtime.start_mcp';
  const source = typeof approval.args.source === 'string' ? approval.args.source : '';
  const origin = source === '~/.codex/config.toml' ? 'Codex' : source === '~/.claude.json' ? 'Claude Code' : source === 'claude desktop' ? 'Claude Desktop' : t('connection.project');
  const details = Object.fromEntries(Object.entries(approval.args).filter(([key]) => key !== 'configFingerprint'));
  return <section className="approval connection-approval" aria-label={t('approval.label')}>
    <strong>{t(mcp ? 'connection.mcpTitle' : 'connection.lspTitle', {name: String(approval.args.server ?? approval.args.command ?? '')})}</strong>
    <p>{t(mcp ? 'connection.mcpDescription' : 'connection.lspDescription')}</p>
    {source && <p><b>{t('connection.source')}</b> {origin} · <code>{source}</code></p>}
    {source === '~/.codex/config.toml' && <p>{t('connection.codex')}</p>}
    <p>{t('connection.effect')}</p>
    <details><summary>{t('connection.details')}</summary><pre>{JSON.stringify(details, null, 2)}</pre></details>
    <div><button onClick={() => respond('deny')}>{t('connection.skip')}</button>{approval.sessionLabel && <button className="session-button" onClick={() => respond('session')}>{t('connection.remember')}</button>}<button className="approve-button" onClick={() => respond('once')}>{t('connection.connect')}</button></div>
    <p className="approval-keys">{t('connection.scope')}</p>
    {remaining !== null && <p className="approval-timeout">{t('approval.timeoutHint', {seconds: remaining})}</p>}
  </section>;
}

function Transcript({thread, preview}: {thread: Thread; preview: (path: string) => void}) {
  const {t} = useI18n();
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [showJump, setShowJump] = useState(false);
  useLayoutEffect(() => {
    const el = scroll.current;
    if (el && follow.current && !window.getSelection()?.toString()) el.scrollTop = el.scrollHeight;
  }, [thread.messages]);
  const jump = () => { follow.current = true; setShowJump(false); const el = scroll.current; if (el) el.scrollTop = el.scrollHeight; };
  return <div className="transcript-wrapper"><div className="transcript" ref={scroll} data-testid="transcript" onScroll={() => { const el = scroll.current!; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; setShowJump(!follow.current); }}>
    {thread.messages.length === 0 ? <div className="empty-conversation"><ProductIcon className="small-brand"/><h1>{t('empty.title')}</h1><p>{t('empty.description')}</p><div className="suggestions">{[t('suggestion.project'), t('suggestion.research'), t('suggestion.story')].map(text => <button key={text} onClick={() => { workspace.patch(thread.id, {draft: text}); void window.rocky.saveDraft(thread.id, text).catch(() => {}); }}>{text}<ChevronRight size={14}/></button>)}</div></div> : <div className="message-list">{thread.messages.map(message => <MessageView key={message.id} message={message} preview={preview}/>)}</div>}
  </div>{showJump && <button className="jump-button" onClick={jump}><ArrowDown size={14}/>{t('jumpLatest')}</button>}</div>;
}

// Markdown renders live during streaming too, re-parsed on a coarse interval —
// per-delta parsing would churn; a final flush lands when the status flips.
function useThrottledText(value: string, active: boolean) {
  const [state, setState] = useState(active ? value : '');
  const latest = useRef(value); latest.current = value;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setState(latest.current), 600);
    return () => clearInterval(timer);
  }, [active]);
  return active ? state : value;
}

const markdownOptions = {remarkPlugins: [remarkGfm], components: {a: ({children}: {children?: React.ReactNode}) => <span className="reference-text">{children}</span>}};

const MessageView = memo(function MessageView({message, preview}: {message: Message; preview: (path: string) => void}) {
  const {t, locale} = useI18n();
  const options = {...markdownOptions, urlTransform: (url: string, key: string) => key === 'href' && /\.html?(?:[?#].*)?$/i.test(url) ? url : defaultUrlTransform(url), components: {a: ({href, children}: {href?: string; children?: React.ReactNode}) => href && /\.html?(?:[?#].*)?$/i.test(href) ? <button className="artifact-link" onClick={() => preview(href)}>{children}</button> : <span className="reference-text">{children}</span>}};
  const streaming = message.status === 'streaming';
  const live = useThrottledText(message.content, streaming);
  if (message.role === 'user') return <article className="user-message"><div>{message.content}</div></article>;
  return <article className="assistant-message"><div className="assistant-label"><ProductIcon/> Rocky</div>
    {!!message.reasoning && <details className="reasoning"><summary>{t('reasoning')}</summary><p>{message.reasoning}</p></details>}
    {message.tools.map(tool => <details className="tool-call" key={tool.id}><summary>{tool.status === 'running' ? <LoaderCircle size={13} className="spin"/> : tool.decision === 'parked' ? <CirclePause size={13}/> : tool.status === 'error' ? <X size={13}/> : <Check size={13}/>}<span>{tool.name}</span>{tool.detail && <code title={tool.detail}>{tool.detail}</code>}<small>{tool.status === 'running' ? t('tool.running') : tool.status === 'cancelled' ? t('tool.cancelled') : tool.status === 'interrupted' ? t('tool.interrupted') : tool.decision === 'parked' ? t('tool.parked') : tool.status === 'error' ? t('tool.error') : t('tool.complete')}</small></summary>{tool.decision && tool.decision !== 'parked' && <p className={`tool-decision ${tool.decision}`}>{tool.decision === 'blocked' ? t('tool.blocked') : tool.decision === 'auto_allowed' ? t('tool.autoAllowed') : t('tool.autoDenied')}</p>}{tool.output && <pre>{tool.output}</pre>}</details>)}
    <div className="prose">{streaming
      ? <div className="streaming-text">{live ? <Markdown {...options}>{live}</Markdown> : <span className="waiting-text">{t('status.running')}<span className="dot-pulse">…</span></span>}</div>
      : <Markdown {...options}>{message.content}</Markdown>}</div>
    {message.status === 'cancelled' && <p className="message-receipt">{t('message.cancelled')}</p>}
    {message.status === 'interrupted' && <p className="message-receipt">{t('message.interrupted')}</p>}
    {message.error && <p className="inline-error">{localizeKeyed(locale, message.error, message.error_key, message.error_params)}</p>}
  </article>;
});

function Composer({id, busy, cancelling, available, modelsKey, report}: {id: string; busy: boolean; cancelling: boolean; available: boolean; modelsKey: string; report: (error: unknown) => void}) {
  const {t} = useI18n();
  const thread = useThread(id)!;
  const [submitting, setSubmitting] = useState(false);
  const [presetsOpen, setPresetsOpen] = useState(false);
  const [modeHelp, setModeHelp] = useState(false);
  const [delivery, setDelivery] = useState<DeliveryMode>(() => localStorage.getItem('rocky.deliveryMode') === 'interrupt' ? 'interrupt' : 'queue');
  const textarea = useRef<HTMLTextAreaElement>(null);
  const edit = (text: string) => { workspace.patch(id, {draft: text}); void window.rocky.saveDraft(id, text).catch(report); };
  const send = async () => {
    if (cancelling || submitting || !available || !thread.draft.trim()) return;
    const prompt = thread.draft; setSubmitting(true);
    try { await window.rocky.startTurn(id, prompt, delivery); if (workspace.get(id)?.draft === prompt) edit(''); }
    catch (error) { report(error); } finally { setSubmitting(false); textarea.current?.focus(); }
  };
  const pending = thread.delivery?.pending ?? [];
  return <div className="composer-area">
    {pending.length > 0 && <div className="message-queue" aria-label={t('queue.label')}>
      <div><strong>{t('queue.count', {count: pending.length})}{thread.delivery?.paused ? t('queue.pausedSuffix') : ''}</strong>
        {thread.delivery?.paused && <button disabled={!available || busy} onClick={() => void window.rocky.resumeQueue(id).catch(report)}>{t('queue.resume')}</button>}</div>
      {pending.map(message => <div key={message.id}><span>{message.prompt}</span><button aria-label={t('queue.withdrawLabel', {prompt: message.prompt.slice(0, 30)})} onClick={() => void window.rocky.removeQueued(id, message.id).catch(report)}>{t('queue.withdraw')}</button></div>)}
    </div>}
    {modeHelp && <section className="mode-help" aria-label={t('mode.help')}><strong>{t('mode.help')}</strong><dl>{(['careful', 'ask', 'yolo'] as const).map(mode => <React.Fragment key={mode}><dt>{t(`mode.${mode}`)}</dt><dd>{t(`mode.${mode}Detail`)}</dd></React.Fragment>)}</dl><p>{t('mode.boundary')}</p><p>{t('mode.codex')}</p></section>}
    <p className="mode-summary">{t(`mode.${thread.permissionMode ?? 'ask'}Detail`)}</p>
    <ModelPicker key={modelsKey} id={id} disabled={busy || !available} report={report}/>
    {presetsOpen && <PresetPanel draft={thread.draft} onApply={body => { edit(body); setPresetsOpen(false); }} onClose={() => setPresetsOpen(false)} report={report}/>}
    <div className="composer-box"><textarea ref={textarea} value={thread.draft} onChange={e => edit(e.target.value)} placeholder={busy ? t('composer.busyDelivery') : t('composer.placeholder')} aria-label={t('composer.label')} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }}/>
      <div className="composer-bottom"><div className="composer-meta"><span className="composer-folder"><FolderOpen size={13}/>{basename(thread.workspace)}</span><PresetButton open={presetsOpen} onToggle={() => setPresetsOpen(value => !value)}/></div><div className="delivery-controls">
        <select aria-label={t('mode.label')} title={t('mode.hint')} value={thread.permissionMode ?? 'ask'} onChange={e => void window.rocky.setPermissionMode(id, e.target.value as PermissionMode).catch(report)}>
          <option value="careful">{t('mode.careful')}</option>
          <option value="ask">{t('mode.ask')}</option>
          <option value="yolo">{t('mode.yolo')}</option>
        </select>
        <button className="mode-help-button" aria-label={t('mode.help')} aria-expanded={modeHelp} onClick={() => setModeHelp(value => !value)}><CircleHelp size={15}/></button>
        <select aria-label={t('delivery.label')} value={delivery} onChange={e => { const mode = e.target.value as DeliveryMode; setDelivery(mode); localStorage.setItem('rocky.deliveryMode', mode); }}><option value="queue">{t('delivery.queue')}</option><option value="interrupt">{t('delivery.interrupt')}</option></select>
        {busy && <button className="stop-button" aria-label={t('composer.stopLabel')} disabled={cancelling} onClick={() => void window.rocky.cancelTurn(id).catch(report)}><Square size={12} fill="currentColor"/>{cancelling ? t('composer.stopping') : t('composer.stop')}</button>}
        <button className="send-button" aria-label={busy ? delivery === 'queue' ? t('composer.sendQueued') : t('composer.sendInterrupt') : t('composer.send')} disabled={submitting || cancelling || !available || !thread.draft.trim()} onClick={() => void send()}>{submitting ? <LoaderCircle size={17} className="spin"/> : <ArrowUp size={18}/>}</button>
      </div></div>
    </div><div className="composer-hint"><span>{available ? (thread.permissionMode === 'yolo' ? t('mode.yoloHint') : t('composer.shortcut')) : t('composer.configure')}</span><span>{delivery === 'queue' ? t('delivery.hintQueue') : t('delivery.hintInterrupt')}</span></div>
  </div>;
}

function NoteEditor({id, close, report}: {id: string; close: () => void; report: (error: unknown) => void}) {
  const {t} = useI18n();
  const thread = useThread(id)!;
  const [state, setState] = useState<'saved' | 'saving' | 'failed'>('saved');
  const revision = useRef(0);
  const edit = (text: string) => {
    workspace.patch(id, {note: text}); setState('saving'); const current = ++revision.current;
    void window.rocky.saveNote(id, text).then(() => { if (current === revision.current) setState('saved'); }).catch(error => { setState('failed'); report(error); });
  };
  return <aside className="note-panel"><header><span><FileText size={16}/>{t('note.title')}</span><button onClick={close} aria-label={t('note.close')}><PanelRightClose size={17}/></button></header><div className="note-caption">{t('note.caption')}</div><textarea value={thread.note} onChange={e => edit(e.target.value)} placeholder={t('note.placeholder')} aria-label={t('note.title')} spellCheck={false}/><footer><span>{state === 'saved' && <Check size={12}/>}<span data-testid="note-save-status">{t(`note.${state}`)}</span></span><span>{t('note.characters', {count: thread.note.length})}</span></footer></aside>;
}

createRoot(document.getElementById('root')!).render(<I18nProvider><App/></I18nProvider>);
