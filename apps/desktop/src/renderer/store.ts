import { useSyncExternalStore } from 'react';
import type { RuntimeEvent, RuntimeInfo, Snapshot, Thread } from '../shared/protocol';

type Listener = () => void;
export class WorkspaceStore {
  private threads = new Map<string, Thread>();
  private listeners = new Map<string, Set<Listener>>();
  private indexListeners = new Set<Listener>();
  private index: Thread[] = [];
  private deltas: Extract<RuntimeEvent, {type: 'delta'}>[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  runtime: RuntimeInfo | null = null;
  hydrate(snapshot: Snapshot) {
    this.runtime = snapshot.runtime;
    for (const thread of snapshot.threads) this.threads.set(thread.id, thread);
    this.updateIndex();
  }
  get = (id: string) => this.threads.get(id);
  getIndex = () => this.index;
  subscribeIndex = (fn: Listener) => { this.indexListeners.add(fn); return () => { this.indexListeners.delete(fn); }; };
  subscribe(id: string, fn: Listener) {
    const listeners = this.listeners.get(id) ?? new Set<Listener>();
    listeners.add(fn); this.listeners.set(id, listeners);
    return () => { listeners.delete(fn); if (!listeners.size) this.listeners.delete(id); };
  }
  private notify(id: string) { this.listeners.get(id)?.forEach(fn => fn()); }
  private updateIndex() {
    this.index = [...this.threads.values()].sort((a, b) => b.updatedAt - a.updatedAt);
    this.indexListeners.forEach(fn => fn());
  }
  patch(id: string, patch: Partial<Thread>) {
    const thread = this.threads.get(id);
    if (!thread) return;
    this.threads.set(id, {...thread, ...patch}); this.notify(id);
  }
  remove(id: string) {
    if (!this.threads.delete(id)) return;
    this.deltas = this.deltas.filter(event => event.threadId !== id);
    this.updateIndex();
  }
  accept(event: RuntimeEvent) {
    if (event.type === 'disconnected' || event.type === 'race' || event.type === 'update') return;
    if (event.type === 'delta') {
      this.deltas.push(event);
      if (!this.timer) this.timer = setTimeout(() => this.flush(), 32);
    } else {
      this.flush();
      const local = this.get(event.threadId);
      // Draft and note are local editable state, not stream-owned content.
      this.threads.set(event.threadId, local ? {...event.thread, draft: local.draft, note: local.note} : event.thread);
      this.notify(event.threadId); this.updateIndex();
    }
  }
  flush() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    const changed = new Set<string>();
    for (const event of this.deltas) {
      const thread = this.get(event.threadId);
      if (!thread || thread.runId !== event.runId) continue;
      const messages = thread.messages.map(message => message.id === event.messageId ? {...message, [event.field]: (message[event.field] ?? '') + event.delta} : message);
      this.threads.set(thread.id, {...thread, messages}); changed.add(thread.id);
    }
    this.deltas = [];
    changed.forEach(id => this.notify(id));
  }
}
export const workspace = new WorkspaceStore();
export function useThread(id: string) {
  return useSyncExternalStore(fn => workspace.subscribe(id, fn), () => workspace.get(id));
}
export function useThreads() { return useSyncExternalStore(workspace.subscribeIndex, workspace.getIndex); }
