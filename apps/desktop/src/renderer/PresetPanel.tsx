import {useEffect, useState} from 'react';
import {Bookmark, Pencil, Trash2, X} from 'lucide-react';
import {draftAfterApply, needsApplyConfirm, normalizePresetName, presetSavePayload, type PromptPreset} from '../shared/presets';
import {localizeKeyed} from '../shared/diagnostics';
import {useI18n} from './i18n';
import {presetStore, usePresets} from './preset-store';

type Editor = {id?: string; name: string; body: string};

const preview = (body: string) => {
  const flat = body.replace(/\s+/g, ' ').trim();
  return flat.length > 80 ? `${flat.slice(0, 80)}…` : flat;
};

export function PresetButton({open, onToggle}: {open: boolean; onToggle: () => void}) {
  const {t} = useI18n();
  return <button type="button" className={`preset-button ${open ? 'active' : ''}`} aria-expanded={open} aria-label={t('preset.open')} onClick={onToggle}><Bookmark size={13}/>{t('preset.button')}</button>;
}

export function PresetPanel({draft, onApply, onClose, report}: {draft: string; onApply: (body: string) => void; onClose: () => void; report: (error: unknown) => void}) {
  const {t, locale} = useI18n();
  const presets = usePresets();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [pending, setPending] = useState<PromptPreset | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);
  const fail = (error: unknown) => { setBusy(false); report(error); };
  const save = async () => {
    if (!editor || busy) return;
    const name = normalizePresetName(editor.name);
    if (name && presets.some(item => item.id !== editor.id && item.name.toLowerCase() === name.toLowerCase())) {
      report(new Error(t('diagnostic.presetDuplicate')));
      return;
    }
    let payload;
    try { payload = presetSavePayload(editor); }
    catch (error) { report(error instanceof Error ? new Error(localizeKeyed(locale, error.message, error.message)) : error); return; }
    setBusy(true);
    try {
      const result = await window.rocky.savePreset(payload);
      presetStore.replace(result.presets);
      setEditor(null);
      setBusy(false);
    } catch (error) { fail(error); }
  };
  const remove = async (id: string) => {
    if (busy) return;
    setBusy(true);
    try {
      const result = await window.rocky.deletePreset(id);
      presetStore.replace(result.presets);
      setConfirmDelete(null);
      setBusy(false);
    } catch (error) { fail(error); }
  };
  const usePreset = (preset: PromptPreset) => {
    if (needsApplyConfirm(draft, preset.body)) setPending(preset);
    else onApply(draftAfterApply(preset.body));
  };
  const duplicate = editor
    ? presets.some(item => item.id !== editor.id && item.name.toLowerCase() === normalizePresetName(editor.name).toLowerCase() && normalizePresetName(editor.name))
    : false;
  let formError = '';
  let payloadOk = false;
  if (editor && !duplicate) {
    try { presetSavePayload(editor); payloadOk = true; }
    catch (error) {
      if (editor.name.trim() || editor.body.trim()) { const message = error instanceof Error ? error.message : ''; formError = localizeKeyed(locale, message, message); }
    }
  }
  const canSave = payloadOk && !busy;
  return <section className="preset-panel" data-testid="preset-panel" role="dialog" aria-label={t('preset.open')}>
    <header><strong>{t('preset.open')}</strong><button type="button" onClick={onClose} aria-label={t('preset.close')}><X size={14}/></button></header>
    <p className="preset-hint">{t('preset.hint')}</p>
    {editor ? <form className="preset-form" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>{t('preset.name')}<input aria-label={t('preset.name')} value={editor.name} maxLength={80} onChange={event => setEditor({...editor, name: event.target.value})} onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void save(); } }}/></label>
      <label>{t('preset.body')}<textarea aria-label={t('preset.body')} value={editor.body} onChange={event => setEditor({...editor, body: event.target.value})} spellCheck={false}/></label>
      {(duplicate || formError) && <p className="inline-error">{duplicate ? t('diagnostic.presetDuplicate') : formError}</p>}
      <div className="preset-form-actions"><button type="button" onClick={() => setEditor(null)}>{t('preset.cancel')}</button><button type="submit" className="preset-save" disabled={!canSave}>{t('preset.save')}</button></div>
    </form> : <>
      <div className="preset-toolbar">
        <button type="button" onClick={() => { setPending(null); setEditor({name: '', body: ''}); }}>{t('preset.new')}</button>
        <button type="button" disabled={!draft.trim()} onClick={() => { setPending(null); setEditor({name: '', body: draft}); }}>{t('preset.saveDraft')}</button>
      </div>
      {pending && <div className="preset-confirm"><p>{t('preset.replace')}</p><strong>{pending.name}</strong><div><button type="button" onClick={() => setPending(null)}>{t('preset.cancel')}</button><button type="button" className="preset-save" onClick={() => onApply(draftAfterApply(pending.body))}>{t('preset.replaceConfirm')}</button></div></div>}
      {presets.length === 0 ? <p className="preset-empty">{t('preset.empty')}</p> : <ul className="preset-list" aria-label={t('preset.open')}>{presets.map(preset => <li key={preset.id}>
        <button type="button" className="preset-use" data-testid="preset-use" aria-label={`${t('preset.use')}: ${preset.name}`} onClick={() => usePreset(preset)}><strong>{preset.name}</strong><small>{preview(preset.body)}</small></button>
        <button type="button" aria-label={`${t('preset.edit')}: ${preset.name}`} onClick={() => { setPending(null); setConfirmDelete(null); setEditor({id: preset.id, name: preset.name, body: preset.body}); }}><Pencil size={13}/></button>
        {confirmDelete === preset.id
          ? <button type="button" className="preset-confirm-delete" onClick={() => void remove(preset.id)}>{t('preset.confirmDelete')}</button>
          : <button type="button" aria-label={`${t('preset.delete')}: ${preset.name}`} onClick={() => setConfirmDelete(preset.id)}><Trash2 size={13}/></button>}
      </li>)}</ul>}
    </>}
  </section>;
}
