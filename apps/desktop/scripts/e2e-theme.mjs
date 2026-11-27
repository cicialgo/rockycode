import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
const root=resolve('.');
const data=await mkdtemp(join(tmpdir(),'rockycode-theme-'));
const preferences=join(data,'ui-preferences.json');
await mkdir('artifacts',{recursive:true});
// A file written before themes existed: the language stays, the theme starts as System.
await writeFile(preferences, JSON.stringify({language:'en'}));
const env={...process.env,ROCKYCODE_DESKTOP_FIXTURE:'1',ROCKYCODE_DESKTOP_DEV:'1',ROCKYCODE_DESKTOP_TEST:'1',ROCKYCODE_DESKTOP_DATA_DIR:data,ROCKYCODE_DESKTOP_TEST_WORKSPACE:data};
delete env.ELECTRON_RUN_AS_NODE;
const saved=async()=>JSON.parse(await readFile(preferences,'utf8'));
const NEBULA={deep:'rgb(18, 18, 25)',bg:'rgb(22, 22, 30)',text:'rgb(192, 202, 245)',violet:'rgb(187, 154, 247)',purple:'rgb(157, 124, 216)'};
const style=(page,selector,property)=>page.locator(selector).first().evaluate((element,name)=>getComputedStyle(element).getPropertyValue(name),property);
// Buttons ease their colours over 120 ms, so a colour is asserted once it has settled.
const settles=async(page,selector,property,expected,message)=>{
 try{await page.waitForFunction(([s,p,e])=>getComputedStyle(document.querySelector(s)).getPropertyValue(p)===e,[selector,property,expected],{timeout:3000});}
 catch{assert.equal(await style(page,selector,property),expected,message);}
};
const theme=page=>page.locator('html').getAttribute('data-theme');
const pressed=(page,name)=>page.getByRole('group',{name:/^(Theme|主题)$/}).getByRole('button',{name}).getAttribute('aria-pressed');
const source=app=>app.evaluate(({nativeTheme})=>nativeTheme.themeSource);
const windowColor=app=>app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].getBackgroundColor().toLowerCase());
async function launch(colorScheme=null){
 // colorScheme:null — Playwright otherwise emulates a light scheme and would hide what the app itself sets.
 const app=await electron.launch({executablePath:electronPath(),args:['.'],cwd:root,env,colorScheme});
 const page=await app.firstWindow();
 // The saved theme must be on <html> as soon as the document exists, before the app has rendered.
 await page.waitForLoadState('domcontentloaded');
 const atLoad=await theme(page);
 await page.locator('.theme-picker button').first().waitFor();
 await page.locator('.primary-button,.send-button').first().waitFor();
 return {app,page,atLoad};
}
let {app,page,atLoad}=await launch();
try{
 // 1. Nothing chosen yet: the app looks as it always did.
 assert.equal(atLoad,null);
 assert.equal(await theme(page),null);
 assert.equal(await pressed(page,'System'),'true');
 assert.equal(await pressed(page,'Nebula'),'false');
 assert.equal(await source(app),'system');
 assert.notEqual(await style(page,'.sidebar','background-color'),NEBULA.deep);
 await page.getByRole('button',{name:'Choose a workspace'}).click();
 await page.getByLabel('Task input').fill('[approval]');
 await page.getByRole('button',{name:'Send task'}).click();
 await page.getByRole('region',{name:'Tool approval'}).waitFor();
 await page.getByLabel('Task input').fill('Keep this draft while the theme changes');
 await page.getByLabel('Toggle notes').click();
 await page.getByLabel('Task notes',{exact:true}).fill('A note the theme must not touch.');
 const before=await page.evaluate(()=>window.rocky.initialize());
 await page.screenshot({path:'artifacts/theme-system-approval.png'});

 // 2. One click on a visible button switches the look; nothing else changes.
 await page.getByRole('button',{name:'Nebula'}).click();
 await page.waitForFunction(()=>document.documentElement.dataset.theme==='nebula');
 await page.getByRole('button',{name:'Nebula',pressed:true}).waitFor();
 assert.equal(await pressed(page,'System'),'false');
 assert.deepEqual(await saved(),{language:'en',theme:'nebula'});
 assert.equal(await source(app),'dark');
 assert.equal(await windowColor(app),'#16161e');
 assert.equal(await page.evaluate(()=>matchMedia('(prefers-color-scheme: dark)').matches),true);
 await settles(page,'.sidebar','background-color',NEBULA.deep);
 await settles(page,'.main-area','background-color',NEBULA.bg);
 await settles(page,'html','color',NEBULA.text);
 await settles(page,'.approval .approve-button','background-color',NEBULA.violet);
 await settles(page,'.status-dot.online','background-color',NEBULA.purple,'Nebula has no green');
 assert.equal(await page.getByLabel('Task input').inputValue(),'Keep this draft while the theme changes');
 assert.equal(await page.getByLabel('Task notes',{exact:true}).inputValue(),'A note the theme must not touch.');
 const after=await page.evaluate(()=>window.rocky.initialize());
 assert.deepEqual(after.threads.map(thread=>[thread.approval,thread.messages]),before.threads.map(thread=>[thread.approval,thread.messages]));
 await page.screenshot({path:'artifacts/theme-nebula-approval.png'});
 await page.getByRole('button',{name:'Deny',exact:true}).click();
 await page.getByLabel('Task input').fill('[long]');
 await page.getByRole('button',{name:'Send task'}).click();
 await page.waitForFunction(()=>document.querySelector('.streaming-text')?.textContent.length>300);
 await page.getByRole('button',{name:'Stop task',exact:true}).click();
 await page.locator('.message-receipt').first().waitFor();
 await page.screenshot({path:'artifacts/theme-nebula-conversation.png'});
 await page.getByTestId('race-nav').click();
 await page.getByTestId('race-panel').waitFor();
 // The race view has no dark rules of its own; Nebula must still own every surface of it.
 await settles(page,'.race-panel','background-color',NEBULA.bg);
 await page.screenshot({path:'artifacts/theme-nebula-race.png'});
 await page.locator('.thread-button').first().click();
 await page.locator('.runtime-pill').click();
 await page.getByRole('dialog').waitFor();
 await page.screenshot({path:'artifacts/theme-nebula-runtime.png'});
 await page.keyboard.press('Escape');
 await page.locator('.modal-backdrop').click({position:{x:5,y:5}}).catch(()=>{});

 // 3. The language switch keeps the theme, and the picker speaks both languages.
 await page.getByRole('combobox',{name:'Interface language'}).selectOption('zh');
 await page.getByRole('group',{name:'主题'}).waitFor();
 assert.equal(await pressed(page,'星云'),'true');
 assert.equal(await pressed(page,'跟随系统'),'false');
 assert.deepEqual(await saved(),{language:'zh',theme:'nebula'});
 assert.equal(await theme(page),'nebula');
 await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].setSize(900,620));
 await page.waitForFunction(()=>window.innerWidth<=900);
 const overflow=await page.locator('.theme-picker,.theme-picker button').evaluateAll(elements=>elements.some(element=>{const box=element.getBoundingClientRect();const parent=element.closest('.sidebar').getBoundingClientRect();return box.right>parent.right+1||box.left<parent.left-1;}));
 assert.equal(overflow,false,'the picker must fit the narrow sidebar');
 await page.screenshot({path:'artifacts/theme-nebula-zh-narrow.png'});
 await page.getByRole('combobox',{name:'界面语言'}).selectOption('en');
 await page.getByRole('group',{name:'Theme'}).waitFor();

 // 4. A restart opens in the saved theme from the first frame.
 await app.close();
 ({app,page,atLoad}=await launch());
 assert.equal(atLoad,'nebula','the saved theme is applied before the app renders');
 assert.equal(await windowColor(app),'#16161e');
 assert.equal(await source(app),'dark');
 assert.equal(await pressed(page,'Nebula'),'true');
 await settles(page,'.sidebar','background-color',NEBULA.deep);
 await page.screenshot({path:'artifacts/theme-nebula-restart.png'});

 // 5. A failed save keeps the current look and says why.
 await rm(preferences);
 await mkdir(preferences);
 await page.getByRole('button',{name:'System'}).click();
 await page.getByRole('alert').filter({hasText:'Could not save the theme'}).waitFor();
 assert.equal(await theme(page),'nebula');
 assert.equal(await pressed(page,'Nebula'),'true');
 assert.equal(await source(app),'dark');
 await rm(preferences,{recursive:true});
 await writeFile(preferences, JSON.stringify({language:'en',theme:'nebula'}));

 // 6. Back to System: the attribute goes, the OS decides again.
 await page.getByRole('button',{name:'System'}).click();
 await page.waitForFunction(()=>!document.documentElement.hasAttribute('data-theme'));
 await page.getByRole('button',{name:'System',pressed:true}).waitFor();
 assert.deepEqual(await saved(),{language:'en',theme:'system'});
 assert.equal(await source(app),'system');
 assert.notEqual(await style(page,'.sidebar','background-color'),NEBULA.deep);

 // 7. An unknown saved theme falls back to System instead of blocking startup.
 await app.close();
 await writeFile(preferences, JSON.stringify({language:'en',theme:'sepia'}));
 ({app,page,atLoad}=await launch());
 assert.equal(atLoad,null);
 assert.equal(await pressed(page,'System'),'true');
 assert.equal(await source(app),'system');

 // 8. System on a dark OS: every view is dark, the Model Race view included (it had no dark rules).
 await app.close();
 await writeFile(preferences, JSON.stringify({language:'en',theme:'system'}));
 ({app,page}=await launch('dark'));
 assert.equal(await page.evaluate(()=>matchMedia('(prefers-color-scheme: dark)').matches),true);
 await settles(page,'.race-nav','background-color','rgb(34, 31, 43)');
 await page.getByTestId('race-nav').click();
 await page.getByTestId('race-panel').waitFor();
 await settles(page,'.race-panel','background-color','rgb(23, 23, 27)');
 await settles(page,'.race-panel','color','rgb(232, 232, 236)');
 await settles(page,'.race-setup','background-color','rgb(28, 28, 33)');
 await page.screenshot({path:'artifacts/theme-system-dark-race.png'});
 console.log('PASS: default look unchanged + one-click Nebula + palette on chat, approval, race and runtime views + no green');
 console.log('PASS: draft, note, approval and messages untouched + bilingual picker + narrow layout + first-frame restart + failed save + back to System + unknown theme');
 console.log('PASS: System on a dark OS is dark in every view, Model Race included');
 console.log('Fixture only: no paid model calls or actual agent tools. Data: '+data);
}catch(error){
 await page.screenshot({path:'artifacts/theme-failure.png'}).catch(()=>{});
 console.error('Visible error:',await page.locator('[role=alert]').allTextContents().catch(()=>[]));
 throw error;
}finally{await app.close();}
