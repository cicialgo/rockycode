/** UI text only. Never apply translations to prompts, model output or tool data.
 *
 * The catalogs are generated from rockycode/i18n/locales/*.toml (one file per
 * language, shared with the terminal UI) by scripts/i18n_desktop.py. A new
 * language is a new TOML file plus a rerun of that script: it lands in
 * `catalogs`, the picker lists it, and the preference accepts it.
 */
import {catalogs, en} from './catalog';
export * from './catalog';
export type Locale = keyof typeof catalogs;
export type LanguagePreference = 'system' | Locale;
export type TranslationKey = keyof typeof en;
export const locales = Object.keys(catalogs) as Locale[];
export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && Object.hasOwn(catalogs, value);
}
export function isLanguagePreference(value: unknown): value is LanguagePreference {
  return value === 'system' || isLocale(value);
}
export function resolveLocale(preference: LanguagePreference, systemLocale: string): Locale {
  if (preference !== 'system' && isLocale(preference)) return preference;
  const language = systemLocale.toLowerCase().split(/[-_]/)[0];
  return isLocale(language) ? language : 'en';
}
type Placeholders<S extends string> = S extends `${string}{${infer P}}${infer Rest}` ? P | Placeholders<Rest> : never;
export type StaticTranslationKey = {[K in TranslationKey]: [Placeholders<(typeof en)[K]>] extends [never] ? K : never}[TranslationKey];
type Values<K extends TranslationKey> = Record<Placeholders<(typeof en)[K]>, string | number>;
type Arguments<K extends TranslationKey> = [Placeholders<(typeof en)[K]>] extends [never] ? [] : [Values<K>];
export type Translator = <K extends TranslationKey>(key: K, ...args: Arguments<K>) => string;
export function translate<K extends TranslationKey>(locale: Locale, key: K, ...args: Arguments<K>): string {
  // English fallback also protects older cached clients from incomplete catalogs.
  const template: string = catalogs[locale]?.[key] ?? en[key] ?? key;
  const values = args[0] as Record<string, string | number> | undefined;
  return template.replace(/\{(\w+)\}/g, (token, name: string) => values?.[name] === undefined ? token : String(values[name]));
}
/** A key chosen at runtime (the Python runtime's `detail_key`): same lookup and
 * fallback as `translate`, without the compile-time placeholder contract. */
export function translateDynamic(locale: Locale, key: string, values?: Record<string, string | number>): string {
  const template: string | undefined = isTranslationKey(key) ? (catalogs[locale]?.[key] ?? en[key]) : undefined;
  if (template === undefined) return key;
  return template.replace(/\{(\w+)\}/g, (token, name: string) => values?.[name] === undefined ? token : String(values[name]));
}
export function isTranslationKey(value: unknown): value is TranslationKey {
  return typeof value === 'string' && Object.hasOwn(en, value);
}
export function translator(locale: Locale): Translator {
  return (key, ...args) => translate(locale, key, ...args);
}
