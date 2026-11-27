import type {MenuItemConstructorOptions} from 'electron';
import {translate, type Locale} from './i18n';

export interface MenuActions { checkForUpdates?: () => void }

/** Explicit role labels avoid mixing Electron's OS locale with the chosen UI language. */
export function nativeMenuTemplate(locale: Locale, packaged: boolean, actions: MenuActions = {}): MenuItemConstructorOptions[] {
  return [
    {label: 'RockyCode', submenu: [
      {role: 'about', label: translate(locale, 'menu.about')},
      {type: 'separator'},
      // The clickable twin of the sidebar's "Check now"; a manual check ignores the daily throttle.
      {label: translate(locale, 'menu.checkUpdates'), click: () => actions.checkForUpdates?.()},
      {type: 'separator'},
      {role: 'hide', label: translate(locale, 'menu.hide')},
      {role: 'quit', label: translate(locale, 'menu.quit')},
    ]},
    {label: translate(locale, 'menu.edit'), submenu: [
      {role: 'undo', label: translate(locale, 'menu.undo')},
      {role: 'redo', label: translate(locale, 'menu.redo')},
      {type: 'separator'},
      {role: 'cut', label: translate(locale, 'menu.cut')},
      {role: 'copy', label: translate(locale, 'menu.copy')},
      {role: 'paste', label: translate(locale, 'menu.paste')},
      {role: 'selectAll', label: translate(locale, 'menu.selectAll')},
    ]},
    {label: translate(locale, 'menu.window'), submenu: [
      {role: 'minimize', label: translate(locale, 'menu.minimize')},
      {role: 'zoom', label: translate(locale, 'menu.zoom')},
      ...(!packaged ? [{role: 'toggleDevTools' as const, label: translate(locale, 'menu.toggleDevTools')}] : []),
    ]},
  ];
}
