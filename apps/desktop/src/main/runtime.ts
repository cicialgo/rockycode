import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, resolve, delimiter } from 'node:path';
import { EventEmitter } from 'node:events';
import { RpcPeer, type Localize } from './rpc';
import type { RuntimeEvent } from '../shared/protocol';

export class Runtime extends EventEmitter {
  readonly peer: RpcPeer;
  private child: ChildProcessWithoutNullStreams;
  private stopping = false;
  constructor(options: {root: string; resources: string; packaged: boolean; data: string; fixture: boolean; localize?: Localize}) {
    super();
    // Dev layout: this app lives at <repo>/apps/desktop, so the shared core is the
    // repository itself and its .venv. A packaged build infers nothing: it must
    // bundle the runtime or set ROCKYCODE_SOURCE / ROCKYCODE_PYTHON explicitly.
    const repo = resolve(options.root, '../..');
    const source = process.env.ROCKYCODE_SOURCE ||
      (!options.packaged && existsSync(join(repo, 'rockycode/runtime.py')) ? repo : '');
    const venvPython = source ? join(source, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python3') : '';
    const python = process.env.ROCKYCODE_PYTHON || (venvPython && existsSync(venvPython) ? venvPython : 'python3');
    const runtime = join(options.packaged ? options.resources : options.root, 'runtime');
    const env = {...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: [runtime, source, process.env.PYTHONPATH].filter(Boolean).join(delimiter)};
    const args = ['-m', 'rockycode_desktop', '--data-dir', options.data];
    if (options.fixture) args.push('--fixture');
    this.child = spawn(python, args, {cwd: options.root, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true});
    this.peer = new RpcPeer(this.child.stdin, undefined, options.localize);
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', chunk => this.peer.accept(chunk));
    // Drain stderr without printing credentials, environment variables or model output.
    this.child.stderr.on('data', () => {});
    this.child.on('error', () => this.peer.close(new Error(this.peer.text('diagnostic.pythonStart'))));
    this.child.on('exit', () => this.peer.close(new Error(this.peer.text('diagnostic.pythonExit'))));
    this.peer.on('event', (event: RuntimeEvent) => this.emit('event', event));
    this.peer.on('disconnect', (error: Error) => { if (!this.stopping) this.emit('event', {type: 'disconnected', error: error.message}); });
  }
  async stop() {
    this.stopping = true;
    const timeout = setTimeout(() => this.child.kill('SIGKILL'), 20000);
    try { await this.peer.request('shutdown'); } catch { this.child.kill(); }
    finally { clearTimeout(timeout); this.child.stdin.end(); }
  }
}
