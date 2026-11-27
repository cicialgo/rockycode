// Explicit paid Mac smoke test: real Electron/IPC/Python/Runtime/provider.
// The launcher only isolates user Skills/MCP/state and caps model output.
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, mkdir, readFile, realpath} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {isAbsolute, join, resolve} from 'node:path';
import {coreSource,corePython} from './core-paths.mjs';
if (!process.argv.includes('--live')) throw new Error('Pass --live to allow paid model calls.');
const root=resolve('.');
const data=await realpath(await mkdtemp(join(tmpdir(),'rocky-electron-live-')));
const core=coreSource();
const python=corePython(core);
if(!core) throw new Error('Shared Runtime not found at the repository root; set ROCKYCODE_SOURCE.');
if(!isAbsolute(python)) throw new Error('Set ROCKYCODE_PYTHON to an absolute interpreter path (the launcher uses it as a shebang).');
const launcher=join(data,'isolated-python');
await writeFile(join(data,'input.txt'),'Synthetic Electron sample 42\n');
await mkdir('artifacts',{recursive:true});
await mkdir(join(data,'desktop'),{recursive:true});
// Keep selectors independent of the host's system locale.
await writeFile(join(data,'desktop','ui-preferences.json'),JSON.stringify({language:'zh'}));
await writeFile(launcher,`#!${python}
import os, sys, runpy
from pathlib import Path
from rockycode.onboarding import bootstrap_credentials
import rockycode.config as config
bootstrap_credentials()
os.environ['ROCKYCODE_MODEL'] = os.environ.get('ROCKYCODE_MODEL') or config.load()['model']
base = Path(__file__).parent
os.environ['ROCKYCODE_HOME'] = str(base / 'global')
config.GLOBAL_PATH = base / 'global/config.toml'
config.GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
config.GLOBAL_PATH.write_text('language = "zh"\\n')
import rockycode.session as session
session.HOME_ROOT = base / 'global'
session.REGISTRY = session.HOME_ROOT / 'projects.json'
import rockycode_desktop.host as host
OriginalAdapter = host.EngineAdapter
def isolated_adapter(*args):
    return OriginalAdapter(*args, include_user=False, mcp=False, lsp=False, thinking=False, max_tokens=1024, max_steps=4)
host.EngineAdapter = isolated_adapter
assert sys.argv[1:3] == ['-m', 'rockycode_desktop']
sys.argv = sys.argv[2:]
runpy.run_module('rockycode_desktop', run_name='__main__')
`,{mode:0o700});
const env={...process.env,ROCKYCODE_DESKTOP_DEV:'1',ROCKYCODE_SOURCE:core,ROCKYCODE_PYTHON:launcher,ROCKYCODE_DESKTOP_DATA_DIR:join(data,'desktop')};
delete env.ELECTRON_RUN_AS_NODE;
delete env.ROCKYCODE_DESKTOP_FIXTURE;
delete env.ROCKYCODE_DESKTOP_TEST;
let app,page;
const report={mode:'real-electron-real-api',result:'running'};
try {
 app=await electron.launch({executablePath:electronPath(),args:['.'],cwd:root,env});
 page=await app.firstWindow();
 await page.getByRole('button',{name:'选择工作目录'}).waitFor();
 await app.evaluate(({dialog},directory)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[directory]});},data);
 await page.getByRole('button',{name:'选择工作目录'}).click();
 await page.getByLabel('任务输入').fill('这是客户端连通测试。只调用 read_file 读取 input.txt，然后用 write_file 把相同内容写入 output.txt。只使用这两个工具，不调用其他工具。文件写入成功后回复“桌面链路验证完成”。');
 const start=Date.now();
 await page.getByRole('button',{name:'发送任务'}).click();
 let complete=false;
 while (Date.now()-start<120000) {
   const approval=page.getByRole('region',{name:'工具审批'});
   if (await approval.count()) {
     const tool=await approval.locator('p').innerText();
     const args=JSON.parse(await approval.locator('pre').innerText());
     const target=typeof args.path==='string' ? resolve(data,args.path) : '';
     const allow=(tool==='read_file' && [join(data,'input.txt'),join(data,'output.txt')].includes(target)) || (tool==='write_file' && target===join(data,'output.txt'));
     await approval.getByRole('button',{name:allow?'允许一次':'拒绝',exact:true}).click();
   }
   const snapshot=await page.evaluate(()=>window.rocky.initialize());
   const thread=snapshot.threads[0];
   if(thread?.messages.length && !['running','waiting','cancelling'].includes(thread.status)) {
     report.model=snapshot.runtime.model;
     report.status=thread.status;
     report.tools=thread.messages.at(-1).tools.map(t=>({name:t.name,status:t.status}));
     assert.equal(thread.messages.at(-1).status,'complete',thread.messages.at(-1).error);
     complete=true;break;
   }
   await page.waitForTimeout(100);
 }
 assert.ok(complete,'real UI run exceeded 120 seconds');
 assert.ok(existsSync(join(data,'output.txt')),'output file must actually exist');
 assert.equal((await readFile(join(data,'output.txt'),'utf8')).trim(),'Synthetic Electron sample 42');
 report.seconds=(Date.now()-start)/1000;
 await page.screenshot({path:'artifacts/workspace-real-model.png'});
 report.result='passed';
 console.log(JSON.stringify(report));
} catch(error) {
 report.result='failed';report.error=String(error.message).replaceAll(data,'<workspace>');
 await page?.screenshot({path:'artifacts/workspace-real-model-failure.png'}).catch(()=>{});
 throw error;
} finally {
 await app?.close();
 await writeFile('artifacts/electron-live.json',JSON.stringify(report,null,2));
}
