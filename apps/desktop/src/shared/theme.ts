/** The desktop's looks.
 *
 * `system` follows the OS light/dark setting, as the app always did. `nebula`
 * is the terminal's own palette (rockycode/palette.py and the TUI's `rocky`
 * theme) on the desktop. It is a dark theme, so the main process pins the OS
 * colour scheme to dark while it is selected and src/renderer/nebula.css only
 * recolours — a new theme is one more name here and one more stylesheet.
 */
export const themes = ['system', 'nebula'] as const;
export type ThemePreference = (typeof themes)[number];
export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === 'string' && (themes as readonly string[]).includes(value);
}
/** What Electron's nativeTheme.themeSource must be while a theme is active. */
export function colorScheme(theme: ThemePreference): 'system' | 'dark' {
  return theme === 'nebula' ? 'dark' : 'system';
}
/** The window colour behind the first paint; null leaves the OS-dependent default. */
export function windowBackground(theme: ThemePreference): string | null {
  return theme === 'nebula' ? '#16161e' : null;
}
/** The saved theme rides on the renderer's argv, so the preload can put it on
 * <html> before the stylesheet paints anything. */
const ARGUMENT = '--rocky-theme=';
export function themeArgument(theme: ThemePreference): string {
  return ARGUMENT + theme;
}
export function themeFromArguments(argv: readonly string[]): ThemePreference {
  const value = argv.find(argument => argument.startsWith(ARGUMENT))?.slice(ARGUMENT.length);
  return isThemePreference(value) ? value : 'system';
}
/** `system` carries no attribute: the base stylesheet is the system theme. */
export function applyTheme(root: HTMLElement, theme: ThemePreference): void {
  if (theme === 'system') delete root.dataset.theme;
  else root.dataset.theme = theme;
}
