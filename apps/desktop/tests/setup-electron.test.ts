import test from 'node:test';
import assert from 'node:assert/strict';
import {copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const desktop = fileURLToPath(new URL('..', import.meta.url));

// Exercise the real setup entrypoint and dependency gate without downloading or
// executing Electron. Only the installer and macOS icon utility are fixtures.
function installation(relative: string, exitCode = 0) {
  const root = mkdtempSync(join(tmpdir(), 'rocky-electron-setup-'));
  for (const file of ['package.json', 'package-lock.json', 'scripts/setup-electron.mjs',
    'scripts/check-dependencies.mjs', 'scripts/dependency-baseline.json',
    'scripts/known-malicious-versions.json', 'scripts/electron-path.mjs']) {
    mkdirSync(dirname(join(root, file)), {recursive: true});
    copyFileSync(join(desktop, file), join(root, file));
  }
  const electron = join(root, 'node_modules/electron');
  mkdirSync(electron, {recursive: true});
  const version = JSON.parse(readFileSync(join(desktop, 'package.json'), 'utf8')).devDependencies.electron;
  writeFileSync(join(electron, 'package.json'), JSON.stringify({version, type: 'commonjs'}));
  writeFileSync(join(electron, 'install.js'), `
    const fs = require('node:fs'), path = require('node:path');
    if (${exitCode}) { console.error('synthetic installer failure'); process.exit(${exitCode}); }
    const dist = path.join(__dirname, 'dist'), marker = path.join(__dirname, 'path.txt');
    // Electron's own installer accepts only its native path as a cache hit;
    // a branded macOS path causes another extraction on repeat setup.
    if (fs.existsSync(marker) && fs.readFileSync(marker, 'utf8') === ${JSON.stringify(relative)}
        && fs.existsSync(path.join(dist, ${JSON.stringify(relative)}))) process.exit(0);
    const executable = path.join(dist, ${JSON.stringify(relative)});
    fs.mkdirSync(path.dirname(executable), {recursive: true});
    fs.writeFileSync(executable, 'synthetic executable; never run');
    fs.writeFileSync(path.join(dist, 'version'), ${JSON.stringify(version)});
    fs.writeFileSync(marker, ${JSON.stringify(relative)});
    if (${JSON.stringify(relative)}.startsWith('Electron.app/')) {
      fs.mkdirSync(path.join(dist, 'Electron.app/Contents/Resources'), {recursive: true});
      fs.writeFileSync(path.join(dist, 'Electron.app/Contents/Info.plist'),
        '<plist><dict><key>CFBundleName</key><string>Electron</string><key>CFBundleDisplayName</key><string>Electron</string><key>CFBundleExecutable</key><string>Electron</string><key>CFBundleIconFile</key><string>electron.icns</string></dict></plist>');
    }
  `);
  mkdirSync(join(root, 'assets/RockyCode.iconset'), {recursive: true});
  writeFileSync(join(root, 'setup-fixture.mjs'), `
    import child from 'node:child_process';
    import {writeFileSync} from 'node:fs';
    import {syncBuiltinESMExports} from 'node:module';
    const realSpawn = child.spawnSync;
    child.spawnSync = (command, args, options) => {
      if (command !== 'iconutil') return realSpawn(command, args, options);
      writeFileSync(args.at(-1), 'synthetic icon');
      return {status: 0};
    };
    syncBuiltinESMExports();
    await import('./scripts/setup-electron.mjs');
  `);
  return {
    root, electron,
    run: () => spawnSync(process.execPath, ['setup-fixture.mjs'], {cwd: root, encoding: 'utf8'}),
    resolve: () => spawnSync(process.execPath, ['--input-type=module', '-e',
      "import {electronPath} from './scripts/electron-path.mjs'; console.log(electronPath());"], {cwd: root, encoding: 'utf8'}),
    close: () => rmSync(root, {recursive: true, force: true}),
  };
}

test('fresh macOS setup updates the renamed plist and remains usable on repeat setup', () => {
  const fixture = installation('Electron.app/Contents/MacOS/Electron');
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const result = fixture.run();
      assert.equal(result.status, 0, result.stdout + result.stderr);
      assert.equal(readFileSync(join(fixture.electron, 'path.txt'), 'utf8').trim(), 'RockyCode.app/Contents/MacOS/RockyCode');
      const resolved = fixture.resolve();
      assert.equal(resolved.status, 0, resolved.stderr);
      assert.ok(existsSync(resolved.stdout.trim()));
      const plist = readFileSync(join(fixture.electron, 'dist/RockyCode.app/Contents/Info.plist'), 'utf8');
      for (const key of ['CFBundleName', 'CFBundleDisplayName', 'CFBundleExecutable']) {
        assert.ok(plist.includes(`<key>${key}</key><string>RockyCode</string>`), key);
      }
      assert.ok(plist.includes('<key>CFBundleIconFile</key><string>RockyCode.icns</string>'));
    }
  } finally { fixture.close(); }
});

test('Linux and Windows setup preserve the installer executable path', () => {
  for (const relative of ['electron', 'electron.exe']) {
    const fixture = installation(relative);
    try {
      const result = fixture.run();
      assert.equal(result.status, 0, result.stdout + result.stderr);
      assert.equal(readFileSync(join(fixture.electron, 'path.txt'), 'utf8').trim(), relative);
      const resolved = fixture.resolve();
      assert.equal(resolved.status, 0, resolved.stderr);
      assert.ok(existsSync(resolved.stdout.trim()));
    } finally { fixture.close(); }
  }
});

test('an installer failure exits with its status before reading or rewriting path.txt', () => {
  const fixture = installation('electron', 23);
  try {
    const result = fixture.run();
    assert.equal(result.status, 23, result.stdout + result.stderr);
    assert.match(result.stderr, /synthetic installer failure/);
    assert.doesNotMatch(result.stderr, /ENOENT/);
    assert.equal(existsSync(join(fixture.electron, 'path.txt')), false);
  } finally { fixture.close(); }
});
