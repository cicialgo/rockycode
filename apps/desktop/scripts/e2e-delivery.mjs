import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
const root=resolve('.');
const data=await mkdtemp(join(tmpdir(),'rocky-delivery-e2e-'));
// Selectors below are Chinese: pin the UI language, as e2e.mjs does.
await writeFile(join(data,'ui-preferences.json'), JSON.stringify({language:'zh'}));
await mkdir('artifacts',{recursive:true});
const env={...process.env,ROCKYCODE_DESKTOP_FIXTURE:'1',ROCKYCODE_DESKTOP_DEV:'1',ROCKYCODE_DESKTOP_TEST:'1',ROCKYCODE_DESKTOP_DATA_DIR:data,ROCKYCODE_DESKTOP_TEST_WORKSPACE:data};
delete env.ELECTRON_RUN_AS_NODE;
async function launch(){
 const app=await electron.launch({executablePath:electronPath(),args:['.'],cwd:root,env});
 const page=await app.firstWindow();
 await page.getByRole('button',{name:'选择工作目录'}).or(page.getByRole('button',{name:'发送任务',exact:true})).first().waitFor();
 return {app,page};
}
let {app,page}=await launch();
try{
 await page.getByRole('button',{name:'选择工作目录'}).click();
 await page.getByLabel('任务输入').fill('first [long]');
 await page.getByRole('button',{name:'发送任务',exact:true}).click();
 await page.getByRole('button',{name:'停止任务',exact:true}).waitFor();
 await page.getByLabel('忙时发送方式').selectOption('queue');
 await page.getByLabel('任务输入').fill('撤回这条');
 await page.getByRole('button',{name:'排队发送',exact:true}).click();
 await page.getByRole('button',{name:'撤回：撤回这条',exact:true}).click();
 await page.locator('.message-queue').waitFor({state:'hidden'});
 await page.getByLabel('任务输入').fill('second [long]');
 await page.getByRole('button',{name:'排队发送',exact:true}).click();
 await page.getByLabel('排队消息').waitFor();
 await page.getByLabel('忙时发送方式').selectOption('interrupt');
 await page.getByLabel('任务输入').fill('urgent [long]');
 await page.getByRole('button',{name:'打断并发送',exact:true}).click();
 await page.getByText('已停止 · 已生成的内容保留').first().waitFor();
 await page.locator('.user-message').filter({hasText:'urgent [long]'}).waitFor();
 assert.equal(await page.getByLabel('排队消息').getByText('second [long]',{exact:true}).count(),1);
 await page.getByRole('button',{name:'停止任务',exact:true}).click();
 await page.getByRole('button',{name:'恢复队列',exact:true}).waitFor();
 await page.screenshot({path:'artifacts/message-delivery-paused.png'});
 await app.close();
 ({app,page}=await launch());
 await page.getByRole('button',{name:'恢复队列',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'停止任务',exact:true}).count(),0,'restart must not replay submitted work');
 assert.equal(await page.getByLabel('忙时发送方式').inputValue(),'interrupt','selection survives restart');
 await page.getByRole('button',{name:'恢复队列',exact:true}).click();
 await page.getByRole('button',{name:'停止任务',exact:true}).waitFor();
 await page.locator('.user-message').filter({hasText:'second [long]'}).waitFor();
 await page.screenshot({path:'artifacts/message-delivery-resumed.png'});
 await page.getByRole('button',{name:'停止任务',exact:true}).click();
 console.log('PASS: queue, withdraw, interrupt, preserve FIFO, pause, restart without replay, resume, saved choice');
}catch(error){
 await page.screenshot({path:'artifacts/message-delivery-failure.png'}).catch(()=>{});
 throw error;
}finally{await app.close();}
