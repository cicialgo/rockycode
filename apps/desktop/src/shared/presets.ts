/** Named prompt presets. Keep the limits aligned with rockycode_desktop/presets.py. */
export const PRESET_NAME_LIMIT = 80;
export const PRESET_BODY_LIMIT = 100_000;
const PRESET_ID = /^[a-f0-9]{32}$/;

export interface PromptPreset {
  id: string;
  name: string;
  body: string;
  updatedAt: number;
}

export function normalizePresetName(name: string): string {
  return name.trim().replace(/\s+/g, ' ');
}

export function isPresetId(value: unknown): value is string {
  return typeof value === 'string' && PRESET_ID.test(value);
}

/** Client-side check before IPC. The desktop host validates again and is the source of truth. */
export function presetSavePayload(input: {id?: unknown; name?: unknown; body?: unknown}): {id?: string; name: string; body: string} {
  if (typeof input.name !== 'string') throw new Error('diagnostic.presetName');
  const name = normalizePresetName(input.name);
  if (!name || name.length > PRESET_NAME_LIMIT) throw new Error('diagnostic.presetName');
  if (typeof input.body !== 'string' || !input.body.trim() || input.body.length > PRESET_BODY_LIMIT) throw new Error('diagnostic.presetBody');
  const payload: {id?: string; name: string; body: string} = {name, body: input.body};
  if (input.id !== undefined && input.id !== '') {
    if (!isPresetId(input.id)) throw new Error('diagnostic.invalidRequest');
    payload.id = input.id;
  }
  return payload;
}

/** Using a preset replaces the composer draft. The saved text becomes the next message. */
export function draftAfterApply(body: string): string {
  return body;
}

/** Ask before discarding a draft that is not already this preset. */
export function needsApplyConfirm(current: string, body: string): boolean {
  return current.length > 0 && current !== body;
}
