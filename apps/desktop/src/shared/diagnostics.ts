import {isTranslationKey, translateDynamic, type Locale} from './i18n';

/** A message from the Python runtime or host, localized by its catalog key.
 *
 * Runtime text crosses the wire as a key plus parameters beside an English
 * fallback (`detail` + `detail_key`, `error` + `error_key`, a JSON-RPC error's
 * `data.key`). The key is rendered from the exported catalog when known; the
 * English text is shown as sent for a key this build does not know. Nothing
 * is ever guessed from the sentence, and user, model and tool content never
 * passes through here. */
export function localizeKeyed(locale: Locale, text: string, key?: string | null, params?: Record<string, string | number>): string {
  if (key && isTranslationKey(key)) return translateDynamic(locale, key, params);
  return text;
}

/** A capability's detail from `Runtime.describe()`: shared `capability_detail.*` keys. */
export function localizeCapabilityDetail(locale: Locale, cap: {detail: string; detail_key?: string | null; params?: Record<string, string | number>}): string {
  return localizeKeyed(locale, cap.detail, cap.detail_key, cap.params);
}
