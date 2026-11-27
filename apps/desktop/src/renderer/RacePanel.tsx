import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { Flag, FolderOpen, LoaderCircle, Play, RotateCcw, Square, X } from 'lucide-react';
import { useI18n } from './i18n';
import type { Race, RaceLane, RaceLimits, RaceModel } from '../shared/race';
import {isTranslationKey, translateDynamic, type Locale, type Translator} from '../shared/i18n';
import {localizeKeyed} from '../shared/diagnostics';
import {blocksNewRace, finishBoard, hasDuplicateModels, humanDraftPayload, mergeLoadedRaces, raceAliases, raceIsRunning, replaceRace, restoreHumanDraft, reviewIsRevealed, validHumanDraft, validRaceLimits, type DraftScore} from './race-state';
import './race.css';

type Preview = {json: string; digest: string; warning: string; warning_key?: string | null};
const aliases = raceAliases;
const initialLimits: RaceLimits = {maxSteps: 6, maxTokens: 2048, timeoutSeconds: 120};
const isRunning = raceIsRunning;
const cleanError = (error: unknown) => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error);
const emptyScores = restoreHumanDraft;

export function RacePanel({hidden, connected}: {hidden: boolean; connected: boolean}) {
  const {locale, t} = useI18n();
  const [races, setRaces] = useState<Race[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<{id: string; message: string} | null>(null);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [models, setModels] = useState<RaceModel[]>([]);
  const [fixture, setFixture] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState('');
  const [choices, setChoices] = useState<string[]>(['', '', '', '', '']);
  const [folder, setFolder] = useState<string | null>(null);
  const [limits, setLimits] = useState<RaceLimits>(initialLimits);
  const [pending, setPending] = useState<string | null>(null);
  const [judgeModel, setJudgeModel] = useState('');
  const [scores, setScores] = useState<Record<string, DraftScore>>(emptyScores);
  const [dirty, setDirty] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [exportReceipt, setExportReceipt] = useState<'saved' | 'cancelled' | null>(null);
  const selected = races.find(race => race.id === selectedId);
  const results = useRef<HTMLElement>(null);
  const upsert = useCallback((race: Race) => setRaces(current => replaceRace(current, race)), []);
  useEffect(() => {
    let live = true;
    const unlisten = window.rocky.onEvent(event => { if (event.type === 'race' && live) upsert(event.race); });
    void window.rocky.listRaces().then(result => {
      if (!live) return;
      setRaces(current => mergeLoadedRaces(current, result.races));
      setModels(result.models); setFixture(result.fixture); setNextCursor(result.nextCursor);
      const laneModels = result.models.filter(model => model.id !== 'fixture:judge');
      setChoices(Array.from({length: 5}, (_, index) => laneModels[index]?.id ?? ''));
      setJudgeModel(result.models.find(model => model.id === 'fixture:judge')?.id ?? result.models[0]?.id ?? '');
      setSelectedId(current => current ?? [...result.races].sort((a, b) => b.createdAt - a.createdAt)[0]?.id ?? null);
    }).catch(reason => { if (live) {setError(cleanError(reason)); setLoadFailed(true);} }).finally(() => { if (live) setLoading(false); });
    return () => {live = false; unlisten();};
  }, [upsert]);
  useEffect(() => {
    const next = restoreHumanDraft(selected?.human);
    setScores(next); setDirty(false); setRevealed(false); setDetailError(null); setPreview(null); setReviewed(false); setExportReceipt(null);
    // Only changing the selected race resets the user's unsaved score draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);
  useEffect(() => {
    if (hidden || !connected || !selected || selected.detailsLoaded) return;
    const id = selected.id;
    let live = true;
    setDetailError(null);
    void window.rocky.getRace(id).then(race => {if (live) upsert(race);}).catch(reason => {
      if (live) setDetailError({id, message: cleanError(reason)});
    });
    return () => {live = false;};
    // Full race events also satisfy the detail request and cancel stale responses.
  }, [hidden, connected, selectedId, selected?.detailsLoaded, detailAttempt, upsert]);
  useEffect(() => {
    if (selected?.detailsLoaded && !dirty) setScores(restoreHumanDraft(selected.human));
    // Only the first detail hydration may initialize a score draft. Later events
    // must not overwrite edits while the user is reviewing this same race.
  }, [selectedId, selected?.detailsLoaded]);
  useEffect(() => {setPreview(null); setReviewed(false);}, [selected]);
  const unavailable = loading || loadFailed || !connected;
  const laneModels = models.filter(model => model.id !== 'fixture:judge');
  const duplicate = hasDuplicateModels(choices, models);
  const limitsValid = validRaceLimits(limits);
  const anyRunning = races.some(blocksNewRace);
  const busy = isRunning(selected);
  const judgeBusy = selected?.judge.status === 'running';
  const showResults = reviewIsRevealed(selected, revealed);
  const validScores = validHumanDraft(scores);
  const act = async (name: string, work: () => Promise<void>) => {
    if (pending) return;
    setPending(name); setError('');
    try {await work();} catch (reason) {setError(cleanError(reason));} finally {setPending(null);}
  };
  const start = () => act('start', async () => {
    if (unavailable || !prompt.trim() || duplicate || choices.some(choice => !choice) || !limitsValid || anyRunning) return;
    const race = await window.rocky.startRace({prompt, models: choices, workspace: folder, limits});
    upsert(race); setSelectedId(race.id);
    // The lanes are below the setup form: bring them into view, or the race runs unseen.
    requestAnimationFrame(() => results.current?.scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}));
  });
  const chooseFolder = () => act('folder', async () => {const result = await window.rocky.chooseWorkspace(); if (result) setFolder(result);});
  const score = () => act('score', async () => {
    if (!selected?.detailsLoaded || !validScores) return;
    const values = humanDraftPayload(scores);
    upsert(await window.rocky.scoreRace(selected.id, values)); setDirty(false);
  });
  const loadOlder = () => act('history', async () => {
    if (!nextCursor) return;
    const result = await window.rocky.listRaces(nextCursor);
    setRaces(current => mergeLoadedRaces(current, result.races));
    setNextCursor(result.nextCursor);
  });
  const state = (value: string) => isTranslationKey('race.state.' + value) ? translateDynamic(locale, 'race.state.' + value) : value;
  return <section className="race-panel" data-testid="race-panel" hidden={hidden} aria-label={t('race.nav')}>
    <header className="race-heading"><div><span className="race-eyebrow">ROCKYCODE / MODEL RACE</span><h1><Flag size={30} aria-hidden="true"/>{t('race.title')}</h1><p>{t('race.subtitle')}</p></div></header>
    {error && <div className="race-error" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label={t('race.dismissError')}><X size={15}/></button></div>}
    {unavailable && <p className="race-notice">{loading ? t('race.loading') : !connected ? t('race.disconnected') : loadFailed ? t('race.historyFailed') : t('race.configureRuntime')}</p>}
    <section className="race-setup" aria-label={t('race.setupLabel')}>
      <div className="race-section-heading"><h2>{t('race.setupHeading')}</h2><span className="race-local">{t('race.notUploaded')}</span></div>
      <label className="race-label" htmlFor="race-task">{t('race.sharedTask')}</label>
      <textarea id="race-task" data-testid="race-prompt" className="race-prompt" value={prompt} maxLength={20000} onChange={event => setPrompt(event.target.value)} placeholder={t('race.promptPlaceholder')}/>
      <div className="race-folder"><button disabled={unavailable || !!pending} onClick={() => void chooseFolder()}><FolderOpen size={14}/>{t('race.addFolder')}</button><span title={folder ?? ''}>{folder ? folder.split(/[/\\]/).filter(Boolean).pop() : t('race.promptOnly')}</span>{folder && <button onClick={() => setFolder(null)} aria-label={t('race.removeFolder')}><X size={13}/></button>}</div>
      <p className="race-help">{t('race.folderHelp')}</p>
      <p className="race-help">{t('race.laneHelp')}</p>
      {folder && <p className="race-warning">{t('race.folderWarning')}</p>}
      <div className="race-model-grid">{choices.map((choice, index) => <label key={index} className="race-model-choice"><span><Flag size={14} aria-hidden="true"/>{t('race.contender')} {index + 1}</span><select data-testid={`race-model-${index}`} aria-label={`${t('race.contender')} ${index + 1}`} value={choice} onChange={event => setChoices(current => current.map((item, at) => at === index ? event.target.value : item))} disabled={unavailable || !!pending || anyRunning}><option value="">{t('race.chooseModel')}</option>{laneModels.map(model => <option key={model.id} value={model.id}>{model.label}</option>)}</select></label>)}</div>
      {duplicate && <p className="race-warning" role="alert">{t('race.duplicateModels')}</p>}
      {!loading && laneModels.length < 5 && <p className="race-warning">{t('race.needFiveModels')}</p>}
      <div className="race-limits">{([{key: 'maxSteps', label: t('race.limitSteps'), min: 1, max: 12}, {key: 'maxTokens', label: t('race.limitTokens'), min: 256, max: 4096}, {key: 'timeoutSeconds', label: t('race.limitSeconds'), min: 10, max: 300}] as const).map(limit => <label key={limit.key}>{limit.label}<input type="number" min={limit.min} max={limit.max} step="1" value={Number.isNaN(limits[limit.key]) ? '' : limits[limit.key]} onChange={event => setLimits(current => ({...current, [limit.key]: event.target.value === '' ? NaN : Number(event.target.value)}))} disabled={anyRunning || !!pending}/><small>{limit.min}–{limit.max}</small></label>)}</div>
      {!limitsValid && <p className="race-warning">{t('race.limitsInvalid')}</p>}
      <div className="race-start-row"><p className="race-charge">{fixture ? t('race.fixtureCharge') : t('race.charge', {steps: limits.maxSteps || '—'})}</p><button className="race-primary" data-testid="race-start" onClick={() => void start()} disabled={unavailable || !!pending || anyRunning || !prompt.trim() || duplicate || choices.some(choice => !choice) || !limitsValid}><Play size={14}/>{pending === 'start' ? t('race.starting') : t('race.start')}</button></div>
    </section>
    <section className="race-results" ref={results} aria-label={t('race.resultsLabel')}>
      <div className="race-section-heading"><h2>{t('race.resultsHeading')}</h2><label className="race-history-label">{t('race.history')}<select data-testid="race-history" disabled={!!pending} value={selectedId ?? ''} onChange={event => setSelectedId(event.target.value || null)}><option value="" disabled>{t('race.noRaces')}</option>{races.map(race => <option key={race.id} value={race.id}>{new Date(race.createdAt).toLocaleString(t('language.tag'), {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})} · {race.prompt.slice(0, 48)} · {state(race.status)}</option>)}</select></label></div>
      {nextCursor && <button className="race-secondary" data-testid="race-load-older" disabled={!!pending || unavailable} onClick={() => void loadOlder()}>{pending === 'history' ? t('race.loadingHistory') : t('race.loadOlder')}</button>}
      {!selected ? <div className="race-empty"><div aria-hidden="true">{aliases.map(alias => <Flag key={alias} size={26}/>)}</div><p>{t('race.emptyTitle')}</p><small>{t('race.emptyHint')}</small></div> : !selected.detailsLoaded ? <div className="race-notice" data-testid="race-detail-loading" role="status">
        {detailError?.id === selected.id ? <><p>{t('race.detailFailed')}</p><p>{detailError.message}</p><button className="race-secondary" data-testid="race-detail-retry" disabled={!connected} onClick={() => setDetailAttempt(value => value + 1)}>{t('race.detailRetry')}</button></> : <p>{t('race.detailLoading')}</p>}
      </div> : <>
        <div className="race-summary"><div><strong data-testid="race-status" data-status={selected.status}>{state(selected.status)}</strong><span>{selected.lanes.filter(lane => lane.status === 'complete').length}/5 {t('race.finishedCount')}</span><span>{selected.includedFiles} {t('race.inputFiles')} · {selected.excludedFiles} {t('race.excluded')}</span><span className="race-local">{t('race.notUploaded')}</span></div>{(busy || judgeBusy) && <button className="race-stop" data-testid="race-cancel" disabled={!!pending || selected.status === 'cancelling'} onClick={() => void act('cancel', async () => {await window.rocky.cancelRace(selected.id);})}><Square size={12}/>{t('race.cancelAll')}</button>}</div>
        <p className="race-task-summary">{selected.prompt}</p>
        {selected.status === 'interrupted' && <p className="race-warning">{t('race.interrupted')}</p>}
        <FinishBoard key={selected.id} race={selected} t={t} state={state} reveal={showResults} models={models}/>
        <p className="race-help">{t('race.aliasHelp')}</p>
        <div className="race-lanes" aria-label={t('race.lanesLabel')}>{[...selected.lanes].sort((a, b) => a.alias.localeCompare(b.alias)).map(lane => <Lane key={lane.id} lane={lane} t={t} locale={locale} state={state} reveal={showResults} modelLabel={models.find(model => model.id === lane.model)?.label ?? lane.model}/>)}</div>
        <section className="race-review"><div className="race-section-heading"><h2>{t('race.reviewHeading')}</h2><span>{selected.human.length ? t('race.humanSaved') : t('race.humanSeparate')}</span></div><p className="race-help">{t('race.reviewHelp')}</p>
          <div className="race-score-grid">{aliases.map(alias => <div className="race-score-card" key={alias}><label>{t('race.lane')} {alias}<input data-testid={`race-human-score-${alias}`} type="number" aria-label={`${t('race.humanScore')} ${alias}`} min="0" max="10" step="0.5" placeholder={t('race.abstain')} value={scores[alias].score} onChange={event => {setScores(current => ({...current, [alias]: {...current[alias], score: event.target.value}})); setDirty(true);}} disabled={busy || judgeBusy || !!pending}/></label><textarea data-testid={`race-human-reason-${alias}`} aria-label={`${t('race.humanReason')} ${alias}`} placeholder={t('race.reasonPlaceholder')} value={scores[alias].reason} maxLength={1000} onChange={event => {setScores(current => ({...current, [alias]: {...current[alias], reason: event.target.value}})); setDirty(true);}} disabled={busy || judgeBusy || !!pending}/></div>)}</div>
          <div className="race-review-actions"><button data-testid="race-save-human" className="race-secondary" disabled={busy || judgeBusy || unavailable || !!pending || !validScores} onClick={() => void score()}>{t('race.saveHuman')}</button>{dirty && <span>{t('race.unsavedReview')}</span>}{!showResults && <button data-testid="race-reveal-judge" className="race-link" onClick={() => setRevealed(true)}>{t('race.reveal')}</button>}</div>
        </section>
        <section className="race-judge"><div className="race-section-heading"><h2>{t('race.judgeHeading')}</h2><span data-testid="race-judge-status" data-status={selected.judge.status}>{state(selected.judge.status)}</span></div><p className="race-help">{t('race.judgeHelp')}</p><div className="race-judge-controls"><label>{t('race.judgeModel')}<select data-testid="race-judge-model" value={judgeModel} onChange={event => setJudgeModel(event.target.value)} disabled={busy || judgeBusy || !!pending || unavailable}>{models.map(model => <option key={model.id} value={model.id}>{model.label}</option>)}</select></label><button data-testid="race-run-judge" className="race-secondary" disabled={busy || judgeBusy || !!pending || unavailable || !judgeModel || !selected.lanes.some(lane => lane.status === 'complete')} onClick={() => void act('judge', async () => {upsert(await window.rocky.judgeRace(selected.id, judgeModel));})}>{judgeBusy ? <LoaderCircle className="spin" size={14}/> : null}{t('race.runJudge')}</button><span>{fixture ? t('race.offlineFixture') : t('race.paidCall')}</span></div>
          {selected.judge.error && <p className="race-warning">{localizeKeyed(locale, selected.judge.error, selected.judge.error_key)}</p>}
          {selected.judge.scores.length > 0 && (showResults ? <div className="race-judge-scores" data-testid="race-judge-scores">{[...selected.judge.scores].sort((a, b) => a.alias.localeCompare(b.alias)).map(item => <div key={item.alias}><strong>{item.alias} <span>{item.score === null ? t('race.abstain') : `${item.score}/10`}</span></strong><p>{item.reason}</p></div>)}</div> : <p className="race-notice">{t('race.judgeHidden')}</p>)}
        </section>
        <section className="race-export"><div className="race-section-heading"><h2>{t('race.exportHeading')}</h2><span className="race-local">{t('race.notUploaded')}</span></div><p className="race-help">{t('race.exportHelp')}</p><button data-testid="race-preview" className="race-secondary" disabled={busy || judgeBusy || !!pending || unavailable || dirty} onClick={() => void act('preview', async () => {setPreview(await window.rocky.previewRace(selected.id)); setReviewed(false); setExportReceipt(null);})}>{t('race.generatePreview')}</button>{dirty && <p className="race-help">{t('race.saveBeforeExport')}</p>}
          {preview && <div className="race-preview"><p className="race-warning">{localizeKeyed(locale, preview.warning, preview.warning_key)}</p><pre data-testid="race-preview-json" tabIndex={0}>{preview.json}</pre><p className="race-digest">SHA-256: {preview.digest}</p><label className="race-confirm"><input type="checkbox" data-testid="race-export-reviewed" checked={reviewed} onChange={event => setReviewed(event.target.checked)}/>{t('race.exportConfirm')}</label><button data-testid="race-export" className="race-primary" disabled={!reviewed || !!pending || unavailable} onClick={() => void act('export', async () => {const result = await window.rocky.exportRace(selected.id, preview.digest); setExportReceipt(result.saved ? 'saved' : 'cancelled');})}>{t('race.chooseSaveLocation')}</button></div>}
          {exportReceipt && <p className="race-notice" data-testid="race-export-receipt">{exportReceipt === 'saved' ? t('race.savedLocally') : t('race.saveCancelled')}</p>}
        </section>
      </>}
    </section>
  </section>;
}

/** While the race runs, five flags wait at the start. When it is over the board runs the
 * race again in two seconds: each flag goes to where its lane got, and takes its share of
 * the slowest lane's time to get there. Order by time only. */
function FinishBoard({race, t, state, reveal, models}: {race: Race; t: Translator; state: (value: string) => string; reveal: boolean; models: RaceModel[]}) {
  const [run, setRun] = useState(0);
  const board = finishBoard(race);
  if (!board) return null;
  const seconds = (lane: RaceLane) => `${((lane.elapsedMs ?? 0) / 1000).toFixed(1)}s`;
  // The model's own name, not its description: long labels do not fit beside the rails.
  // The full label is the tooltip, and each lane below shows it.
  const name = (lane: RaceLane) => {
    const model = models.find(entry => entry.id === lane.model);
    return {short: model?.model || model?.label || lane.model, label: model?.label ?? lane.model};
  };
  return <section className="race-finish" data-testid="race-finish" data-over={board.over} aria-label={t('race.finishHeading')}>
    <div className="race-section-heading"><h2>{t('race.finishHeading')}</h2>{board.over && <button className="race-link" data-testid="race-replay" onClick={() => setRun(value => value + 1)}><RotateCcw size={11} aria-hidden="true"/>{t('race.replay')}</button>}</div>
    <ol className={board.over ? 'race-finish-rows' : 'race-finish-rows race-finish-waiting'} key={`${board.over}-${run}`} data-run={run}>{board.entries.map((entry, index) => <li key={entry.alias} data-testid={`race-finish-${entry.alias}`} data-place={entry.place ?? ''} className={!board.over ? undefined : entry.place === null ? 'race-finish-out' : entry.place === 1 ? 'race-finish-first' : undefined} style={{'--share': entry.share, '--time': entry.time, '--row': index} as CSSProperties}>
      <span className="race-alias">{entry.alias}</span>
      <div className="race-finish-rail"><Flag className="race-finish-flag" size={15} aria-hidden="true"/></div>
      <span className="race-finish-result">{!board.over ? null : entry.place === null ? state(entry.lane.status) : <><b>{entry.place}</b>{seconds(entry.lane)}</>}</span>
      {reveal && <span className="race-finish-name" title={name(entry.lane).label}>{name(entry.lane).short}</span>}
    </li>)}</ol>
    <p className="race-help">{t(board.over ? 'race.finishHelp' : 'race.finishWaiting')}</p>
  </section>;
}

function Lane({lane, t, locale, state, reveal, modelLabel}: {lane: RaceLane; t: Translator; locale: Locale; state: (value: string) => string; reveal: boolean; modelLabel: string}) {
  return <article className={`race-lane race-lane-${lane.status}`} data-testid={`race-lane-${lane.alias}`} data-status={lane.status}>
    <header><span className="race-alias">{lane.alias}</span><div><strong>{t('race.lane')} {lane.alias}</strong><span>{reveal ? modelLabel : t('race.identityHidden')}</span></div><Flag className="race-horse" size={22} aria-hidden="true"/></header>
    <div className="race-lane-track"><span>{lane.status === 'running' && <LoaderCircle size={12} className="spin"/>}{state(lane.status)}</span><i aria-hidden="true"/></div>
    <dl className="race-metrics"><div><dt>{t('race.elapsed')}</dt><dd>{lane.elapsedMs === null ? '—' : `${(lane.elapsedMs / 1000).toFixed(1)}s`}</dd></div><div><dt>{t('race.tokens')}</dt><dd>{lane.inputTokens ?? '—'} / {lane.outputTokens ?? '—'}</dd></div><div><dt>{t('race.cost')}</dt><dd>{lane.costUsd === null ? '—' : `$${lane.costUsd.toFixed(4)}`}</dd></div></dl>
    <div className="race-lane-output" tabIndex={0}><pre>{lane.content || t('race.waitingOutput')}</pre>{lane.error && <p className="race-warning">{localizeKeyed(locale, lane.error, lane.error_key)}</p>}</div>
    {lane.tools.length > 0 && <details className="race-tool-results"><summary>{t('race.toolActivity')} ({lane.tools.length})</summary>{lane.tools.map((tool, index) => <p key={`${tool.name}-${index}`}><strong>{tool.name}</strong><span>{state(tool.status)}</span></p>)}</details>}
    <div className="race-file-results"><strong>{t('race.resultFiles')} ({lane.files.length})</strong>{!!lane.files.some(file => file.omittedFiles) && <p className="race-warning">{t('race.filesOmitted', {count: lane.files.reduce((total, file) => total + (file.omittedFiles ?? 0), 0)})}</p>}{lane.files.length === 0 ? <small>{t('race.noFiles')}</small> : lane.files.map((file, index) => <details key={`${file.path}-${index}`}><summary>{file.path}{file.truncated ? t('race.partialPreview') : ''}</summary><pre>{file.content}</pre>{file.truncated && <p className="race-warning">{t('race.fileTruncated')}</p>}{!!file.omittedFiles && <p className="race-warning">{t('race.moreFilesOmitted', {count: file.omittedFiles})}</p>}</details>)}</div>
  </article>;
}
