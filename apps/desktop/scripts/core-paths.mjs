// Shared-core locations for the dev layout: this app lives at <repo>/apps/desktop,
// so the core is the repository root and its .venv. ROCKYCODE_SOURCE and
// ROCKYCODE_PYTHON override both for any other layout.
import {existsSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
export const repoRoot=resolve(fileURLToPath(new URL('../../..',import.meta.url)));
export function coreSource(){return process.env.ROCKYCODE_SOURCE || (existsSync(join(repoRoot,'rockycode/runtime.py'))?repoRoot:'');}
export function corePython(source=coreSource()){
 const venv=source?join(source,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python3'):'';
 return process.env.ROCKYCODE_PYTHON || (venv&&existsSync(venv)?venv:'python3');
}
