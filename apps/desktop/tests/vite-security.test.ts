import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm, realpath} from 'node:fs/promises';
import {join, dirname} from 'node:path';
import {tmpdir} from 'node:os';
import {createServer, type ViteDevServer} from 'vite';

test('Vite denies an absolute host path that collides with a root-relative import', {
  // GHSA-rq7h-c2jc-7f22 affects non-Windows dev servers. Use only synthetic
  // files and loopback; never read a real host file or invoke an editor.
  skip: process.platform === 'win32',
}, async () => {
  // macOS tmpdir lives behind a symlink (/var → /private/var); Vite compares
  // realpaths against fs.allow, so anchor the fixture on the real path.
  const temp = await mkdtemp(join(await realpath(tmpdir()), 'rocky-vite-boundary-'));
  const root = join(temp, 'project');
  const outside = join(temp, 'outside.js');
  const decoy = root + outside;
  let server: ViteDevServer | undefined;
  try {
    await mkdir(dirname(decoy), {recursive: true});
    await writeFile(outside, 'export default "OUTSIDE_FIXTURE";');
    await writeFile(decoy, 'export default "PROJECT_DECOY";');
    await writeFile(join(root, 'main.js'), `import text from ${JSON.stringify('.' + outside)}; console.log(text);`);
    server = await createServer({configFile: false, root, server: {
      host: '127.0.0.1', port: 0, fs: {strict: true, allow: [root]},
    }});
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === 'object');
    const origin = `http://127.0.0.1:${address.port}`;
    const imported = await fetch(origin + '/main.js', {signal: AbortSignal.timeout(5000)});
    assert.equal(imported.status, 200);
    assert.ok((await imported.text()).includes(outside), 'exercise the root-relative collision');
    const escaped = await fetch(origin + '/@fs' + outside, {signal: AbortSignal.timeout(5000)});
    assert.equal(escaped.status, 403);
    assert.ok(!(await escaped.text()).includes('OUTSIDE_FIXTURE'));
  } finally {
    await server?.close();
    await rm(temp, {recursive: true, force: true});
  }
});
