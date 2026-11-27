import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import type { Writable } from 'node:stream';
import {localizeKeyed} from '../shared/diagnostics';

/** `key` is a desktop catalog key; `fallback` is the English text sent with it.
 * Returns the key's text in the active language when the key is known, else the fallback (or the key). */
export type Localize = (key: string, fallback?: string, params?: Record<string, string | number>) => string;
export const localizeEnglish: Localize = (key, fallback, params) => localizeKeyed('en', fallback ?? key, key, params);

export class RpcPeer extends EventEmitter {
  private buffer = '';
  private closed = false;
  private pending = new Map<string, { resolve: (value: any) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  constructor(private input: Writable, private timeout = 30_000, private localize: Localize = localizeEnglish) { super(); }
  /** The active-language text for one of the peer's own catalog keys. */
  text(key: string): string { return this.localize(key); }
  request<T = unknown>(method: string, params: unknown = {}): Promise<T> {
    if (this.closed) return Promise.reject(new Error(this.localize('diagnostic.disconnected')));
    const id = randomUUID();
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(this.localize('diagnostic.timeout'))); }, this.timeout);
      this.pending.set(id, {resolve, reject, timer});
      this.input.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\n', 'utf8', error => {
        if (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
      });
    });
  }
  accept(chunk: string) {
    this.buffer += chunk;
    if (Buffer.byteLength(this.buffer) > 8_000_000) return this.close(new Error(this.localize('diagnostic.messageTooLarge')));
    let split: number;
    while ((split = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, split); this.buffer = this.buffer.slice(split + 1);
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        if (!msg || msg.jsonrpc !== '2.0') throw new Error(this.localize('diagnostic.invalidProtocol'));
        if (msg.id !== undefined) {
          const pending = this.pending.get(msg.id);
          if (!pending) continue;
          clearTimeout(pending.timer); this.pending.delete(msg.id);
          if (msg.error) pending.reject(new Error(this.hostError(msg.error)));
          else pending.resolve(msg.result);
        } else if (msg.method === 'event') this.emit('event', msg.params);
      } catch { this.close(new Error(this.localize('diagnostic.malformedProtocol'))); }
    }
  }
  /** A JSON-RPC error from the Python host: `data.key` + `data.params` beside the English `message`. */
  private hostError(error: {message?: unknown; data?: {key?: unknown; params?: unknown}}): string {
    const message = String(error.message || '');
    const key = typeof error.data?.key === 'string' ? error.data.key : undefined;
    const params = error.data?.params && typeof error.data.params === 'object' ? error.data.params as Record<string, string | number> : undefined;
    if (key) return this.localize(key, message || undefined, params);
    return message || this.localize('diagnostic.requestFailed');
  }
  close(error = new Error(this.localize('diagnostic.runtimeExited'))) {
    if (this.closed) return;
    this.closed = true;
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(error); }
    this.pending.clear(); this.emit('disconnect', error);
  }
}
