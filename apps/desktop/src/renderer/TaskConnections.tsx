import React, {useState} from 'react';
import {useThread, useThreads, workspace} from './store';
import {useI18n} from './i18n';

export function TaskConnections({id, connected, openTask, report}: {
  id: string; connected: boolean; openTask: (id: string) => void; report: (error: unknown) => void;
}) {
  const {t} = useI18n();
  const thread = useThread(id)!;
  const threads = useThreads();
  const peers = threads.filter(row => row.id !== id && row.workspace === thread.workspace);
  const [prompt, setPrompt] = useState('');
  const [message, setMessage] = useState('');
  const [target, setTarget] = useState('');
  const [pending, setPending] = useState(false);
  const [receipt, setReceipt] = useState('');
  const targetExists = peers.some(row => row.id === target);
  const create = async () => {
    setPending(true);
    try {
      const child = await window.rocky.createTask(id, prompt);
      workspace.accept({type: 'thread', threadId: child.id, runId: null, thread: child});
      openTask(child.id);
    } catch (error) { report(error); } finally { setPending(false); }
  };
  const send = async () => {
    setPending(true);
    try {
      await window.rocky.sendTaskMessage(id, target, message);
      setMessage(''); setReceipt(t('tasks.sent'));
    } catch (error) { report(error); } finally { setPending(false); }
  };
  return <details className="task-connections" data-testid="task-connections">
    <summary>{t('tasks.title')} · {thread.taskInbox?.length ?? 0}</summary>
    <p>{t('tasks.hint')}</p>
    <div className="task-links">{peers.filter(row => row.id === thread.parentTaskId || row.parentTaskId === id).map(row =>
      <button key={row.id} onClick={() => openTask(row.id)}>{row.id === thread.parentTaskId ? t('tasks.parent') : t('tasks.child')}: {row.title || t('newTask')}</button>)}</div>
    <label>{t('tasks.handoff')}<textarea aria-label={t('tasks.handoff')} value={prompt} maxLength={20000} onChange={event => setPrompt(event.target.value)}/></label>
    <button data-testid="create-linked-task" disabled={!connected || pending || !prompt.trim()} onClick={() => void create()}>{t('tasks.create')}</button>
    <label>{t('tasks.target')}<select aria-label={t('tasks.target')} value={targetExists ? target : ''} onChange={event => setTarget(event.target.value)}>
      <option value="">{t('tasks.choose')}</option>{peers.map(row => <option key={row.id} value={row.id}>{row.title || t('newTask')} · {row.id.slice(0, 8)}</option>)}
    </select></label>
    <textarea aria-label={t('tasks.message')} placeholder={t('tasks.message')} value={message} maxLength={20000} onChange={event => setMessage(event.target.value)}/>
    <button disabled={!connected || pending || !targetExists || !message.trim()} onClick={() => void send()}>{t('tasks.send')}</button>
    {receipt && <p role="status">{receipt}</p>}
    <ol className="task-inbox">{thread.taskInbox?.map(item => <li key={item.id}>
      <strong>{peers.find(row => row.id === item.sender)?.title || item.sender}</strong><pre>{item.content}</pre>
      <button disabled={!connected || pending} onClick={() => {
        const text = `${t('tasks.peerNote', {id: item.sender})}\n${item.content}`;
        const draft = [workspace.get(id)?.draft, text].filter(Boolean).join('\n\n');
        workspace.patch(id, {draft});
        void window.rocky.saveDraft(id, draft).catch(report);
      }}>{t('tasks.use')}</button>
      <button disabled={!peers.some(row => row.id === item.sender)} onClick={() => setTarget(item.sender)}>{t('tasks.reply')}</button>
      <button disabled={!connected || pending} onClick={() => void window.rocky.dismissTaskMessage(id, item.id).catch(report)}>{t('tasks.dismiss')}</button>
    </li>)}</ol>
  </details>;
}
