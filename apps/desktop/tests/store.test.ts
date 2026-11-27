import {test} from 'node:test';
import assert from 'node:assert/strict';
import {WorkspaceStore} from '../src/renderer/store';
import type {Thread} from '../src/shared/protocol';
const thread = (): Thread => ({id:'t',title:'test',workspace:'/tmp',updatedAt:1,messages:[{id:'m',role:'assistant',content:'',status:'streaming',tools:[]}],note:'',draft:'',status:'running',runId:'current',approval:null,tools:[]});
function setup() { const s = new WorkspaceStore(); s.hydrate({protocolVersion:1,runtime:{mode:'fixture',available:true,model:'fixture',version:'1',limitations:[]},threads:[thread()]}); return s; }
test('deltas are batched and stale run events cannot corrupt current output', () => {
  const s=setup(); let changes=0; let indexChanges=0;
  s.subscribe('t',()=>changes++);s.subscribeIndex(()=>indexChanges++);
  for(const [runId,delta] of [['old','wrong'],['current','你'],['current','好']])s.accept({type:'delta',threadId:'t',runId,messageId:'m',field:'content',delta});
  s.flush();assert.equal(s.get('t')!.messages[0].content,'你好');assert.equal(changes,1);assert.equal(indexChanges,0);
});
test('terminal snapshots flush queued text and preserve current user edits',()=>{
  const s=setup();s.patch('t',{draft:'下一条草稿',note:'我的原稿'});
  s.accept({type:'delta',threadId:'t',runId:'current',messageId:'m',field:'content',delta:'partial'});
  const complete={...thread(),status:'idle' as const,messages:[{...thread().messages[0],status:'complete' as const,content:'final'}]};
  s.accept({type:'thread',threadId:'t',runId:'current',thread:complete});s.flush();
  assert.equal(s.get('t')!.messages[0].content,'final');assert.equal(s.get('t')!.draft,'下一条草稿');assert.equal(s.get('t')!.note,'我的原稿');
});
