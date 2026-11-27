import test from 'node:test';
import assert from 'node:assert/strict';
import {PreviewDocuments, artifactCsp} from '../src/main/artifacts';

test('static previews deny script execution without rewriting interactive originals', () => {
  const directives = new Map(artifactCsp.split(';').map(rule => {
    const [name, ...values] = rule.trim().split(/\s+/);
    return [name, values.join(' ')];
  }));
  for (const name of ['default-src', 'script-src', 'script-src-attr', 'script-src-elem', 'worker-src', 'connect-src', 'frame-src', 'object-src', 'base-uri', 'form-action']) {
    assert.equal(directives.get(name), "'none'", name);
  }
  assert.equal(directives.get('sandbox'), '');
  assert.equal(directives.get('style-src'), "'unsafe-inline'");
  assert.equal(directives.get('img-src'), 'data:');
  const original = '<style>h1{color:red}</style><h1>Report</h1><button onclick="this.textContent=1">Try</button><script>window.fixture=1</script>';
  const previews = new PreviewDocuments();
  assert.equal(previews.get(previews.add(original)), original);
});

test('preview documents require issued opaque URLs and can be revoked', () => {
  const previews = new PreviewDocuments();
  const url = previews.add('<h1>Local</h1>');
  assert.match(url, /^rocky-artifact:\/\/preview\/[a-f0-9-]{36}$/);
  assert.equal(previews.get(url), '<h1>Local</h1>');
  for (const unissued of ['file:///etc/passwd', url + '/../../secret', url + '?path=/secret', 'https://example.com']) assert.equal(previews.get(unissued), undefined);
  previews.remove(url);
  assert.equal(previews.get(url), undefined);
});

test('retained documents and multibyte HTML size are bounded', () => {
  const previews = new PreviewDocuments();
  const oldest = previews.add('first');
  for (let i = 0; i < 8; i++) previews.add(String(i));
  assert.equal(previews.get(oldest), undefined);
  assert.throws(() => previews.add('中'.repeat(400_000)), /size limit/);
});
