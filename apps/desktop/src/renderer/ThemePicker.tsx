import React, {useEffect, useState} from 'react';
import {applyTheme, isThemePreference, themes, type ThemePreference} from '../shared/theme';
import type {StaticTranslationKey, Translator} from '../shared/i18n';
import './nebula.css';

// Rocky speaks in musical chords: his own theme wears a note.
const marks: Record<ThemePreference, string> = {system: '◐', nebula: '♪'};
const labels: Record<ThemePreference, StaticTranslationKey> = {system: 'theme.system', nebula: 'theme.nebula'};

/** One visible button per theme: a choice is a single click, never a hidden menu or a key. */
export function ThemePicker({t, report}: {t: Translator; report: (error: unknown) => void}) {
  // The preload already put the saved theme on <html>; start from what is on screen.
  const [theme, setTheme] = useState<ThemePreference>(() => {
    const applied = document.documentElement.dataset.theme;
    return isThemePreference(applied) ? applied : 'system';
  });
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let alive = true;
    void window.rocky.getTheme().then(saved => {
      if (alive) { setTheme(saved); applyTheme(document.documentElement, saved); }
    }).catch(() => { /* An unreadable preference keeps the launch theme. */ });
    return () => { alive = false; };
  }, []);
  const choose = async (value: ThemePreference) => {
    if (value === theme || saving) return;
    setSaving(true);
    // Only a saved choice changes the look: a failed write keeps the current theme.
    try {
      const saved = await window.rocky.setTheme(value);
      setTheme(saved);
      applyTheme(document.documentElement, saved);
    } catch (error) { report(error); }
    finally { setSaving(false); }
  };
  return <div className="theme-picker" role="group" aria-label={t('theme.label')}>
    <span>{t('theme.label')}</span>
    <div>{themes.map(value => <button key={value} className={value === theme ? 'selected' : ''} aria-pressed={value === theme} disabled={saving} onClick={() => void choose(value)}><i aria-hidden="true">{marks[value]}</i>{t(labels[value])}</button>)}</div>
  </div>;
}
