import {test} from 'node:test';
import assert from 'node:assert/strict';
import {PassThrough} from 'node:stream';
import {RpcPeer} from '../src/main/rpc';
import {translate, translateDynamic} from '../src/shared/i18n';

test('RPC handles fragmented messages and interleaved events', async () => {
  const stdin = new PassThrough(); let outbound = '';
  stdin.on('data', chunk => {outbound += chunk;});
  const peer = new RpcPeer(stdin, 1000);
  const pending = peer.request('initialize');
  const {id} = JSON.parse(outbound);
  const events: unknown[] = []; peer.on('event', e => events.push(e));
  const wire = JSON.stringify({jsonrpc:'2.0',method:'event',params:{text:'中文'}})+'\n'+JSON.stringify({jsonrpc:'2.0',id,result:{ok:true}})+'\n';
  peer.accept(wire.slice(0, 19)); peer.accept(wire.slice(19));
  assert.deepEqual(await pending,{ok:true}); assert.deepEqual(events,[{text:'中文'}]); peer.close();
});
test('malformed output rejects pending requests instead of hanging', async () => {
  const peer = new RpcPeer(new PassThrough()); const pending = peer.request('initialize');
  const rejection = assert.rejects(pending, /invalid (runtime )?protocol/i); peer.accept('not-json\n'); await rejection;
});
test('disconnect rejects every pending request', async () => {
  const peer = new RpcPeer(new PassThrough());
  const pending = [peer.request('one'),peer.request('two')];
  const assertions = pending.map(p => assert.rejects(p,/The runtime exited/)); peer.close(); await Promise.all(assertions);
});
test('silent runtime has bounded request timeout', async () => {
  const peer = new RpcPeer(new PassThrough(), 10); await assert.rejects(peer.request('silent'),/did not respond in time/); peer.close();
});
test('host errors are localized by their catalog key, English text is the fallback', async () => {
  const reply = (peer: RpcPeer, outbound: {text: string}, error: object) => {
    const {id} = JSON.parse(outbound.text);
    peer.accept(JSON.stringify({jsonrpc:'2.0', id, error}) + '\n');
  };
  const run = async (localeArg: 'en' | 'zh', error: object) => {
    const stdin = new PassThrough(); const outbound = {text: ''};
    stdin.on('data', chunk => {outbound.text += chunk;});
    const peer = new RpcPeer(stdin, 1000, (key, fallback, params) => {
      const known = key === 'diagnostic.sessionMissing' || key === 'diagnostic.requestFailed';
      return known ? translateDynamic(localeArg, key, params) : fallback ?? key;
    });
    const pending = peer.request('thread/rename'); reply(peer, outbound, error);
    try { await pending; return 'resolved'; } catch (reason) { return (reason as Error).message; } finally { peer.close(); }
  };
  // a key the client knows: rendered in the active language, the English message ignored
  assert.equal(await run('zh', {code: -32602, message: 'Session not found', data: {key: 'diagnostic.sessionMissing', params: {}}}), '会话不存在');
  assert.equal(await run('en', {code: -32602, message: 'Session not found', data: {key: 'diagnostic.sessionMissing', params: {}}}), 'Session not found');
  // an unknown key (newer host): the English text as sent
  assert.equal(await run('zh', {code: -32602, message: 'Future refusal', data: {key: 'diagnostic.future', params: {}}}), 'Future refusal');
  // no key (a plain host ValueError): the text as sent; no text at all: the generic refusal
  assert.equal(await run('zh', {code: -32602, message: 'Output limit', data: {key: null}}), 'Output limit');
  assert.equal(await run('zh', {code: -32602, message: ''}), translate('zh', 'diagnostic.requestFailed'));
});
