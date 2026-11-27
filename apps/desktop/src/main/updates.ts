import {mkdirSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {DEFAULT_FEED, FEED_LIMIT, isNewer, parseFeed, parseVersion, type UpdateCheck, type UpdateFeed} from '../shared/updates';

export interface UpdateState { enabled: boolean; lastCheck: number; skipped: string | null }
export const DAY = 24 * 60 * 60 * 1000;
export const FEED_TIMEOUT = 10_000;

/** The slice of fetch() the reminder uses; tests pass a fake. */
export interface FeedResponse { ok: boolean; headers: {get(name: string): string | null}; text(): Promise<string> }
export type FetchFeed = (url: string, init: {signal: AbortSignal; cache: 'no-store'}) => Promise<FeedResponse>;

// The launch check is off until the user turns it on: the feed's host is not up
// yet, and an app that asks a missing host at every launch is noise. "Check
// now" and the menu item work either way.
const DEFAULT_STATE: UpdateState = {enabled: false, lastCheck: 0, skipped: null};

/** The light update reminder: one GET of a small version file, at most once a
 * day, and only once the user has switched the launch check on. It never downloads or installs anything; the only
 * link it opens is the one the last accepted feed named. State lives in the
 * app's user-data directory, beside the UI preferences, never in a workspace. */
export class UpdateReminder {
  private state: UpdateState = {...DEFAULT_STATE};
  private latest: UpdateFeed | null = null;
  private readonly path: string;

  constructor(
    private readonly userData: string,
    private readonly currentVersion: string,
    private readonly feed: string = DEFAULT_FEED,
    private readonly fetchFeed: FetchFeed = (url, init) => globalThis.fetch(url, init),
    private readonly now: () => number = Date.now,
  ) {
    this.path = join(userData, 'update-reminder.json');
    try {
      const saved: unknown = JSON.parse(readFileSync(this.path, 'utf8'));
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        const record = saved as Record<string, unknown>;
        if (typeof record.enabled === 'boolean') this.state.enabled = record.enabled;
        if (typeof record.lastCheck === 'number' && Number.isFinite(record.lastCheck) && record.lastCheck >= 0) this.state.lastCheck = record.lastCheck;
        if (typeof record.skipped === 'string' && parseVersion(record.skipped)) this.state.skipped = record.skipped;
      }
    } catch {
      // Missing, corrupt or unreadable state must not prevent startup.
    }
  }

  get settings(): {enabled: boolean} {
    return {enabled: this.state.enabled};
  }

  /** The off switch. Throws when the choice cannot be saved, so the UI never claims it was. */
  setEnabled(enabled: boolean): {enabled: boolean} {
    if (typeof enabled !== 'boolean') throw new Error('Invalid update preference');
    this.persist({...this.state, enabled});
    return this.settings;
  }

  /** "Later": hide this version until a newer one appears. */
  skip(version: string): void {
    if (!parseVersion(version)) return;
    try { this.persist({...this.state, skipped: version}); } catch { /* best effort; the banner still closes */ }
  }

  /** The link the banner may open: exactly the one the last accepted feed named, nothing else. */
  link(url: string): string | null {
    return this.latest && this.latest.url === url ? url : null;
  }

  /** `manual` (the menu item, "Check now") ignores the daily throttle, the off switch and a skipped version. */
  async check(manual = false): Promise<UpdateCheck> {
    if (!manual) {
      if (!this.state.enabled) return {status: 'skipped'};
      if (this.state.lastCheck && this.now() - this.state.lastCheck < DAY) return {status: 'skipped'};  // 0 = never checked
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FEED_TIMEOUT);
    let feed: UpdateFeed | null;
    try {
      const response = await this.fetchFeed(this.feed, {signal: controller.signal, cache: 'no-store'});
      if (!response.ok) return {status: 'failed'};
      const length = Number(response.headers.get('content-length'));
      if (Number.isFinite(length) && length > FEED_LIMIT) return {status: 'failed'};
      feed = parseFeed(await response.text());
    } catch {
      return {status: 'failed'};
    } finally {
      clearTimeout(timer);
    }
    if (!feed) return {status: 'failed'};
    // Only a feed we could read counts as a check; a failure retries next launch.
    try { this.persist({...this.state, lastCheck: this.now()}); } catch { /* best effort */ }
    this.latest = feed;
    if (!isNewer(feed.version, this.currentVersion)) return {status: 'upToDate', version: this.currentVersion};
    if (!manual && this.state.skipped === feed.version) return {status: 'skipped'};
    return {status: 'available', update: feed};
  }

  private persist(next: UpdateState): void {
    const temporaryPath = `${this.path}.${randomUUID()}.tmp`;
    try {
      mkdirSync(this.userData, {recursive: true});
      writeFileSync(temporaryPath, `${JSON.stringify(next)}\n`, {encoding: 'utf8', mode: 0o600, flag: 'wx'});
      renameSync(temporaryPath, this.path);
    } catch {
      try { rmSync(temporaryPath, {force: true}); } catch { /* best-effort cleanup */ }
      throw new Error('Could not save update preference');
    }
    this.state = next;
  }
}
