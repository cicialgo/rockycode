import {mkdirSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {isLanguagePreference, type LanguagePreference} from '../shared/i18n';
import {isThemePreference, type ThemePreference} from '../shared/theme';
import type {LanguageSettings} from '../shared/protocol';

/** Local UI preferences never go through the Python runtime or a workspace.
 * One file holds them all, so saving one never drops another. */
export class UiPreferences {
  private languagePreference: LanguagePreference = 'system';
  private themePreference: ThemePreference = 'system';
  private readonly path: string;

  constructor(private readonly userData: string, private readonly systemLocale: string) {
    this.path = join(userData, 'ui-preferences.json');
    try {
      const saved: unknown = JSON.parse(readFileSync(this.path, 'utf8'));
      if (saved && typeof saved === 'object') {
        // Each field is validated on its own: one bad value does not discard the other.
        if ('language' in saved && isLanguagePreference(saved.language)) this.languagePreference = saved.language;
        if ('theme' in saved && isThemePreference(saved.theme)) this.themePreference = saved.theme;
      }
    } catch {
      // Missing, corrupt, or unreadable preferences must not prevent startup.
    }
  }

  language(): LanguageSettings {
    return {preference: this.languagePreference, systemLocale: this.systemLocale};
  }

  theme(): ThemePreference {
    return this.themePreference;
  }

  setLanguage(preference: LanguagePreference): LanguageSettings {
    // Validate at the storage boundary as well as the IPC boundary.
    if (!isLanguagePreference(preference)) throw new Error('Invalid language preference');
    this.save(preference, this.themePreference, 'Could not save language preference');
    this.languagePreference = preference;
    return this.language();
  }

  setTheme(preference: ThemePreference): ThemePreference {
    if (!isThemePreference(preference)) throw new Error('Invalid theme preference');
    this.save(this.languagePreference, preference, 'Could not save theme preference');
    this.themePreference = preference;
    return preference;
  }

  private save(language: LanguagePreference, theme: ThemePreference, failure: string): void {
    const temporaryPath = `${this.path}.${randomUUID()}.tmp`;
    try {
      mkdirSync(this.userData, {recursive: true});
      writeFileSync(temporaryPath, `${JSON.stringify({language, theme})}\n`, {encoding: 'utf8', mode: 0o600, flag: 'wx'});
      renameSync(temporaryPath, this.path);
    } catch {
      // Do not report success or change the active preference if persistence fails.
      try { rmSync(temporaryPath, {force: true}); } catch { /* Best-effort cleanup. */ }
      throw new Error(failure);
    }
  }
}
