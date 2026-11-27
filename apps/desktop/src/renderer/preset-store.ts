import {useSyncExternalStore} from 'react';
import type {PromptPreset} from '../shared/presets';

type Listener = () => void;

export class PresetStore {
  private presets: PromptPreset[] = [];
  private listeners = new Set<Listener>();
  hydrate(presets: PromptPreset[] | undefined) {
    this.replace(Array.isArray(presets) ? presets : []);
  }
  replace(presets: PromptPreset[]) {
    this.presets = presets.map(item => ({id: item.id, name: item.name, body: item.body, updatedAt: item.updatedAt}));
    this.listeners.forEach(fn => fn());
  }
  get = () => this.presets;
  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };
}

export const presetStore = new PresetStore();
export function usePresets() {
  return useSyncExternalStore(presetStore.subscribe, presetStore.get);
}
