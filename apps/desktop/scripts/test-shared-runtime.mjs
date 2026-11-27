import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {delimiter,join} from 'node:path';
import {coreSource,corePython} from './core-paths.mjs';
const core=coreSource();
const python=corePython(core);
if(!core||!existsSync(join(core,'rockycode/runtime.py'))) throw new Error('Shared Runtime not found at the repository root; set ROCKYCODE_SOURCE to a checkout containing rockycode/runtime.py.');
const env={...process.env,PYTHONPATH:[core,process.env.PYTHONPATH].filter(Boolean).join(delimiter),PYTHONDONTWRITEBYTECODE:'1'};
// Preflight prevents silently reporting all shared-core tests as skipped.
const probe=spawnSync(python,['-c','from rockycode.runtime import build_runtime'],{env,stdio:'inherit'});
if(probe.status!==0) process.exit(probe.status || 1);
for(const args of [[join(core,'tests/smoke_runtime.py')],['-m','unittest','discover','-s','runtime/tests','-v']]) {
 const result=spawnSync(python,args,{env,stdio:'inherit'});
 if(result.error) throw result.error;
 if(result.status!==0) process.exit(result.status || 1);
}
