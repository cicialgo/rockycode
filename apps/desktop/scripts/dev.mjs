import { spawn } from 'node:child_process';
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
const electron=electronPath();
await import('./build.mjs');
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
// The rebranded dev binary (RockyCode, not Electron) flips app.isPackaged;
// this marker keeps dev/e2e on the unpackaged layout.
env.ROCKYCODE_DESKTOP_DEV = '1';
if (process.argv.includes('--fixture')) env.ROCKYCODE_DESKTOP_FIXTURE = '1';
const child = spawn(electron, ['.'], { stdio: 'inherit', env });
child.on('exit', code => process.exit(code ?? 1));
