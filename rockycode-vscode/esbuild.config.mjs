import * as esbuild from 'esbuild';

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');

/** @type {esbuild.BuildOptions} */
const extensionConfig = {
  entryPoints: ['src/extension.ts'],
  outfile: 'dist/extension.js',
  bundle: true,
  format: 'cjs',
  platform: 'node',
  external: ['vscode'],
  target: 'es2020',
  sourcemap: !production,
  minify: production,
  treeShaking: true,
};

/** The syntax highlighter for the chat webview → media/highlight.js (browser IIFE). */
/** @type {esbuild.BuildOptions} */
const webviewConfig = {
  entryPoints: ['src/webview-highlight.ts'],
  outfile: 'media/highlight.js',
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: 'es2020',
  sourcemap: false,
  minify: true, // it's a shipped asset — always minify
  treeShaking: true,
};

async function main() {
  if (watch) {
    const ctxs = await Promise.all([
      esbuild.context(extensionConfig),
      esbuild.context(webviewConfig),
    ]);
    await Promise.all(ctxs.map((c) => c.watch()));
    console.log('[esbuild] watching for changes...');
  } else {
    await Promise.all([esbuild.build(extensionConfig), esbuild.build(webviewConfig)]);
    console.log('[esbuild] build complete');
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
