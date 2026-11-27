// Explicit, reviewed installer; npm lifecycle scripts remain disabled.
import './check-dependencies.mjs';
import {spawnSync} from 'node:child_process';
import {existsSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const env={};
// Preserve only ordinary OS paths. No npm mirrors, remote-checksum overrides,
// API keys, GitHub/npm tokens, or inherited Electron executable overrides.
for(const name of ['PATH','HOME','USERPROFILE','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','LOCALAPPDATA','APPDATA']) if(process.env[name]) env[name]=process.env[name];
console.log('Preparing the pinned Electron binary from the official release source with bundled checksum verification.');
const result=spawnSync(process.execPath,[fileURLToPath(new URL('../node_modules/electron/install.js',import.meta.url))],{stdio:'inherit',env});
if(result.error) throw result.error;
if(result.status!==0) process.exit(result.status??1);

// Dev identity: the unpackaged binary announces itself as "Electron" in the
// Dock, Spotlight, the app switcher and the process list. Rebrand the LOCAL
// copy — folder, executable, Info.plist and path.txt — so every surface reads
// RockyCode. Packaged builds get their own bundle at build time, and userData
// is pinned in main/index.ts so this never moves user data. NOTE: the renamed
// executable flips app.isPackaged; dev launchers set ROCKYCODE_DESKTOP_DEV to
// keep the unpackaged layout. Idempotent; macOS-only (.app layout).
const dist=new URL('../node_modules/electron/dist/',import.meta.url);
const appDir=new URL('Electron.app/',dist);
const branded=new URL('RockyCode.app/',dist);
if(existsSync(appDir)){
  if(existsSync(branded)) rmSync(branded,{recursive:true,force:true});  // mixed state from a partial rebrand
  const plist=new URL('Contents/Info.plist',appDir);
  const before=readFileSync(plist,'utf8');
  const after=before.replace(/(<key>(?:CFBundleName|CFBundleDisplayName)<\/key>\s*<string>)Electron(<\/string>)/g,'$1RockyCode$2');
  if(after!==before) writeFileSync(plist,after);
  const executable=new URL('Contents/MacOS/Electron',appDir);
  if(existsSync(executable)){
    renameSync(executable,new URL('Contents/MacOS/RockyCode',appDir));
    const plistExec=readFileSync(plist,'utf8');
    writeFileSync(plist,plistExec.replace(/(<key>CFBundleExecutable<\/key>\s*<string>)Electron(<\/string>)/,'$1RockyCode$2'));
  }
  renameSync(appDir,branded);
  // The approved macOS icon (apps/desktop/assets/RockyCode.iconset, PR #44)
  // is assembled into the bundle at install time — the iconset dir stays the
  // single source of truth, no generated icns is committed.
  const iconset=new URL('../assets/RockyCode.iconset',import.meta.url);
  if(existsSync(iconset)){
    const icns=new URL('Contents/Resources/RockyCode.icns',branded);
    const result=spawnSync('iconutil',['-c','icns',fileURLToPath(iconset),'-o',fileURLToPath(icns)],{stdio:'inherit'});
    if(result.status===0){
      const brandedPlist=new URL('Contents/Info.plist',branded);
      const plistIcon=readFileSync(brandedPlist,'utf8');
      writeFileSync(brandedPlist,plistIcon.replace(/(<key>CFBundleIconFile<\/key>\s*<string>)[^<]*(<\/string>)/,'$1RockyCode.icns$2'));
      console.log('Installed the approved RockyCode icon (assets/RockyCode.iconset → icns).');
    }else console.log('iconutil failed; the bundle keeps the stock icon.');
  }
  // Only a renamed macOS bundle changes the installer's executable path.
  writeFileSync(new URL('../node_modules/electron/path.txt',import.meta.url),'RockyCode.app/Contents/MacOS/RockyCode');
  console.log('Rebranded the dev Electron bundle as RockyCode (Dock / Spotlight / process list).');
}
