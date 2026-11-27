import React, {createContext, useCallback, useContext, useEffect, useState} from 'react';
import {resolveLocale, translate, translator, type LanguagePreference, type Locale, type Translator} from '../shared/i18n';
import type {LanguageSettings} from '../shared/protocol';

type I18n = {locale: Locale; t: Translator; preference: LanguagePreference; loading: boolean; error: string; dismissError: () => void; setLanguage: (value: LanguagePreference) => Promise<void>};
const Context = createContext<I18n | null>(null);
export function I18nProvider({children}: {children: React.ReactNode}) {
  const [settings, setSettings] = useState<LanguageSettings>({preference: 'system', systemLocale: navigator.language});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    void window.rocky.getLanguage().then(value => { if (alive) setSettings(value); }).catch(error => { if (alive) setError(error instanceof Error ? error.message : String(error)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);
  const setLanguage = useCallback(async (value: LanguagePreference) => {
    setLoading(true);
    try { setSettings(await window.rocky.setLanguage(value)); setError(''); }
    catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setLoading(false); }
  }, []);
  const locale = resolveLocale(settings.preference, settings.systemLocale);
  useEffect(() => { document.documentElement.lang = translate(locale, 'language.tag'); }, [locale]);
  return <Context.Provider value={{locale, t: translator(locale), preference: settings.preference, loading, error, dismissError: () => setError(''), setLanguage}}>{children}</Context.Provider>;
}
export function useI18n(): I18n {
  const value = useContext(Context);
  if (!value) throw new Error('I18nProvider is required');
  return value;
}
