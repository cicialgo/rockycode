// macOS regression: even a cached stock Electron bundle receives the approved
// Dock icon at startup. No downloads, bundle edits, signing or model calls.
import './check-dependencies.mjs';
import {electronPath} from './electron-path.mjs';
import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp, mkdir, readFile, symlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

if (process.platform !== 'darwin') {
  console.log('SKIP: Dock icon regression requires macOS.');
  process.exit(0);
}
const root = fileURLToPath(new URL('../', import.meta.url));
const data = await mkdtemp(join(tmpdir(), 'rockycode-icon-e2e-'));
const state = join(data, 'state');
await mkdir(state);
await mkdir(join(root, 'artifacts'), {recursive: true});
for (const directory of ['assets', 'runtime']) await symlink(join(root, directory), join(data, directory));
await writeFile(join(data, 'package.json'), JSON.stringify({name: 'rockycode-icon-test', main: 'main.cjs'}));
// Observe the real startup call, forwarding it to Electron's native Dock API.
await writeFile(join(data, 'main.cjs'), `
const {app, nativeImage} = require('electron');
const {readFileSync} = require('node:fs');
const {createHash} = require('node:crypto');
const setIcon = app.dock.setIcon.bind(app.dock);
globalThis.iconCalls = [];
app.dock.setIcon = path => {
  const image = nativeImage.createFromPath(path);
  globalThis.iconCalls.push({path, empty: image.isEmpty(), size: image.getSize(), scales: image.getScaleFactors(),
    sha256: createHash('sha256').update(readFileSync(path)).digest('hex')});
  return setIcon(path);
};
require(${JSON.stringify(join(root, 'dist/main/index.cjs'))});
`);
const env = {...process.env, ROCKYCODE_DESKTOP_DEV: '1', ROCKYCODE_DESKTOP_FIXTURE: '1', ROCKYCODE_DESKTOP_DATA_DIR: state};
delete env.ELECTRON_RUN_AS_NODE;
let app;
try {
  app = await electron.launch({executablePath: electronPath(), args: [data], cwd: root, env});
  const page = await app.firstWindow();
  await page.locator('.language-picker select').waitFor();
  const calls = await app.evaluate(() => globalThis.iconCalls);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, join(data, 'assets/RockyCode.iconset/icon_512x512@2x.png'));
  assert.equal(calls[0].empty, false);
  // Electron interprets @2x filenames as 512 logical points / 1024 pixels.
  assert.deepEqual(calls[0].size, {width: 512, height: 512});
  assert.ok(calls[0].scales.includes(2));
  const expected = createHash('sha256').update(await readFile(join(root, 'assets/RockyCode.iconset/icon_512x512@2x.png'))).digest('hex');
  assert.equal(calls[0].sha256, expected);
  await writeFile(join(root, 'artifacts/icon-e2e.json'), JSON.stringify({result: 'passed', calls}, null, 2));
  console.log('PASS: startup forwards the approved 1024px source image to the real macOS Dock API.');
} finally {
  await app?.close();
}
