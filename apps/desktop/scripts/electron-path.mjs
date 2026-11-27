import {readFileSync,existsSync} from 'node:fs';
import {resolve,sep} from 'node:path';
import {fileURLToPath} from 'node:url';
export function electronPath() {
 const root=fileURLToPath(new URL('../node_modules/electron/',import.meta.url));
 const dist=resolve(root,'dist');
 let relative;
 try {relative=readFileSync(resolve(root,'path.txt'),'utf8').trim();} catch {throw new Error('Electron is not prepared. Review dependencies, then run npm run setup:electron explicitly.');}
 const executable=resolve(dist,relative);
 if(!executable.startsWith(dist+sep)||!existsSync(executable)) throw new Error('Invalid Electron installation. Run npm run setup:electron after review.');
 const installed=readFileSync(resolve(dist,'version'),'utf8').trim().replace(/^v/,'');
 const expected=JSON.parse(readFileSync(resolve(root,'package.json'),'utf8')).version;
 if(installed!==expected) throw new Error('Electron binary/package version mismatch.');
 return executable;
}
