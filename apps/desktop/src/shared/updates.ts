/** The update reminder's data contract (docs/release-runbook.md §5, Stage C).
 *
 * A static JSON file we host names the newest desktop version and where to get
 * it. The app compares that version with its own and shows a banner with the
 * link. Nothing is downloaded or installed by the app itself, so the feed can
 * only ever make the app *say* something — and only within the limits below. */

export interface UpdateFeed {
  /** The newest version, `x.y.z`. */
  version: string;
  /** Where the banner's button goes: https, on one of the hosts we own. */
  url: string;
  /** Optional plain text, at most NOTES_LIMIT characters. */
  notes?: string;
}

export type UpdateCheck =
  | {status: 'available'; update: UpdateFeed}
  | {status: 'upToDate'; version: string}
  | {status: 'failed'}
  /** Nothing was fetched: checks are off, one ran recently, or the version was skipped. */
  | {status: 'skipped'};

export const DEFAULT_FEED = 'https://rockycode.ai/desktop/latest.json';
export const FEED_LIMIT = 64 * 1024;
export const NOTES_LIMIT = 500;
const LINK_HOSTS = new Set(['rockycode.ai', 'www.rockycode.ai', 'github.com']);
const GITHUB_PREFIX = '/cicialgo/rockycode/';
const VERSION = /^v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:[-+][0-9A-Za-z.-]{0,64})?$/;

export function parseVersion(value: unknown): [number, number, number] | null {
  if (typeof value !== 'string') return null;
  const match = VERSION.exec(value.trim());
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

/** True only when both versions parse and `candidate` is strictly newer. */
export function isNewer(candidate: string, current: string): boolean {
  const a = parseVersion(candidate);
  const b = parseVersion(current);
  if (!a || !b) return false;
  for (let index = 0; index < 3; index++) {
    if (a[index] !== b[index]) return a[index] > b[index];
  }
  return false;
}

/** Accept only an https link on a host we own; GitHub links must stay inside our repository. */
export function acceptableLink(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 2048) return null;
  let url: URL;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'https:' || url.username || url.password || !LINK_HOSTS.has(url.hostname)) return null;
  if (url.hostname === 'github.com' && !url.pathname.startsWith(GITHUB_PREFIX)) return null;
  return url.href;
}

/** The feed body → an UpdateFeed, or null for anything that is not exactly what we publish. */
export function parseFeed(text: string): UpdateFeed | null {
  if (typeof text !== 'string' || text.length > FEED_LIMIT) return null;
  let data: unknown;
  try { data = JSON.parse(text); } catch { return null; }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  const version = parseVersion(record.version) ? String(record.version).trim().replace(/^v/, '') : null;
  const url = acceptableLink(record.url);
  if (!version || !url) return null;
  const feed: UpdateFeed = {version, url};
  if (typeof record.notes === 'string') {
    // Plain text only: control characters out, length capped. Rendered as text, never as markup.
    const notes = record.notes.replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, '').trim().slice(0, NOTES_LIMIT);
    if (notes) feed.notes = notes;
  }
  return feed;
}
