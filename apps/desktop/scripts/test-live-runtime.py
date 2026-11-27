"""Opt-in paid smoke test. Uses existing RockyCode credentials, never prints them.

Run with the core's Python and --live. All model-visible files are synthetic;
only explicitly enumerated operations in a temporary workspace are approved.
No dependency install, real project scan, MCP startup or user Skills discovery.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Dev layout: <repo>/apps/desktop → the shared core is the repository root.
CORE = Path(os.environ.get('ROCKYCODE_SOURCE') or ROOT.parents[1]).resolve()
sys.path[:0] = [str(CORE), str(ROOT / 'runtime')]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--live', action='store_true', help='allow paid model requests with configured credentials')
if not parser.parse_args().live:
    parser.error('pass --live to explicitly enable model requests')

# Bootstrap before isolating runtime state; only key presence/model leave this process.
from rockycode.onboarding import bootstrap_credentials
from rockycode.config import load
bootstrap_credentials()
MODEL = os.environ.get('ROCKYCODE_MODEL') or load().get('model')
if not MODEL:
    raise SystemExit('No RockyCode model configured')

REPORT = {'model': MODEL, 'mode': 'real-api', 'stages': []}


async def main():
    with tempfile.TemporaryDirectory(prefix='rocky-live-') as tmp:
        root = Path(tmp).resolve()
        os.environ['ROCKYCODE_HOME'] = str(root / 'global')
        os.environ['ROCKYCODE_MODEL'] = MODEL
        from rockycode_desktop.adapter import EngineAdapter
        from rockycode_desktop.host import Host
        import rockycode.session as session
        # session/config may have been imported by a credential/provider helper.
        import rockycode.config as config
        session.HOME_ROOT = root / 'global'
        session.REGISTRY = session.HOME_ROOT / 'projects.json'
        config.GLOBAL_PATH = root / 'global/config.toml'
        config.GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.GLOBAL_PATH.write_text('language = "zh"\n')
        expected = 'Synthetic sample 42\nRuntime verified\n'
        (root / 'input.txt').write_text('Synthetic sample 42\n')
        skill = root / '.rockycode/skills/runtime-probe/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: runtime-probe\ndescription: Only for the explicit Runtime smoke test.\n---\nRead input.txt, recall the memory named runtime-probe, then write output.txt with the input text followed by the memory body. Do not run tests or other tools.\n')
        memory = root / '.rockycode/memory/facts/runtime-probe.md'
        memory.parent.mkdir(parents=True)
        memory.write_text('---\nname: runtime-probe\ntype: fact\ndescription: Synthetic smoke-test suffix.\n---\nRuntime verified\n')
        (root / 'sleeper.py').write_text('import os,time\nfrom pathlib import Path\nPath("child.pid").write_text(str(os.getpid()))\ntime.sleep(60)\n')
        approved = []
        REPORT['approvals'] = approved
        metrics = []

        def factory(*args):
            adapter = EngineAdapter(*args, include_user=False, mcp=False, lsp=False,
                                    thinking=False, max_tokens=2048, max_steps=6)
            metrics.append(adapter)
            return adapter

        host = Host(str(root / 'desktop-data'))
        thread = await host.dispatch('thread/create', {'workspace': str(root)})
        thread_id = thread['id']
        args = {'threadId': thread_id}

        def allow(stage, tool, data):
            path = data.get('path', '')
            target = (root / path).resolve() if isinstance(path, str) else None
            if stage == 'composition':
                if tool == 'skill':
                    return data.get('name') == 'runtime-probe'
                if tool == 'recall_memory':
                    return data.get('name') == 'runtime-probe' and not data.get('query')
                if tool == 'read_file':
                    return target in {root / 'input.txt', root / 'output.txt', skill, memory}
                if tool == 'write_file':
                    return target == root / 'output.txt' and isinstance(data.get('content'), str) and len(data['content']) < 4096
            if stage == 'cancel' and tool == 'bash':
                return data.get('command', '').strip() == 'python3 sleeper.py'
            return False

        async def turn(stage, prompt, cancel_on_pid=False):
            print('RUN: ' + stage, flush=True)
            start = time.monotonic()
            await host.dispatch('turn/start', {**args, 'prompt': prompt})
            task = host.tasks[thread_id]
            async with asyncio.timeout(180):
                while not task.done():
                    approval = thread['approval']
                    if approval:
                        accepted = allow(stage, approval['tool'], approval['args'])
                        approved.append({'stage': stage, 'tool': approval['tool'], 'allowed': accepted})
                        await host.dispatch('approval/respond', {**args, 'approvalId': approval['id'], 'allowed': accepted})
                    if cancel_on_pid and (root / 'child.pid').exists():
                        await host.dispatch('turn/cancel', args)
                        cancel_on_pid = False
                    await asyncio.sleep(.02)
                await task
            last = thread['messages'][-1]
            from rockycode.engine.redact import redact
            REPORT['last_receipt'] = json.loads(redact(json.dumps(last, ensure_ascii=False)).replace(str(root), '<workspace>'))
            assert last['status'] == ('cancelled' if stage == 'cancel' else 'complete'), last.get('error', last['status'])
            result = {'stage': stage, 'status': last['status'], 'seconds': round(time.monotonic() - start, 2), 'tools': [t['name'] for t in last['tools']]}
            REPORT['stages'].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return last

        with patch('rockycode_desktop.host.EngineAdapter', factory):
            try:
                await turn('composition', '这是共享 Runtime 的受控冒烟测试。请调用 skill(name="runtime-probe")，严格执行它：读取 input.txt、按名字召回 runtime-probe 记忆，把输入文本加一行记忆正文写入 output.txt。完成后只回复“验证完成”。只使用 skill、read_file、recall_memory、write_file，不探索项目，不运行测试，不调用其他工具。')
                actual = (root / 'output.txt').read_text()
                assert [s for s in actual.splitlines() if s.strip()] == expected.splitlines(), 'Output content mismatch'
                used = set(REPORT['stages'][-1]['tools'])
                assert {'skill', 'read_file', 'recall_memory', 'write_file'} <= used, used
                await turn('deny', '第二个审批测试：请尝试调用 write_file 写 denied.txt，内容为 DENIED。如果界面拒绝，立即停止，不改用其他方式，只回复“已拒绝”。')
                assert not (root / 'denied.txt').exists()
                assert any(a['stage'] == 'deny' and a['tool'] == 'write_file' and not a['allowed'] for a in approved)
                await turn('cancel', '第三个取消测试：只调用 bash，command 必须是 python3 sleeper.py。这是已准备好的无害等待程序；运行后等待我按停止。不读取、不改写脚本，不尝试其他命令。', cancel_on_pid=True)
                pid = int((root / 'child.pid').read_text())
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError('Cancelled process still running')
                assert not host.adapters
                await host.dispatch('note/save', {**args, 'content': 'USER_OWNED_NOTE'})
                await host.dispatch('draft/save', {**args, 'content': 'PRESERVED_DRAFT'})
                await host.close()
                host = Host(str(root / 'desktop-data'))
                thread = host.threads[thread_id]
                assert thread['note'] == 'USER_OWNED_NOTE' and thread['draft'] == 'PRESERVED_DRAFT'
                last = await turn('resume', '刚才的等待任务已由用户取消，不要继续执行。现在只回复“恢复成功”，不要调用工具。')
                assert '恢复成功' in last['content']
                REPORT['registered_tools'] = thread['tools']
                REPORT['approvals'] = approved
                REPORT['usage'] = [a.engine.stats.usage for a in metrics]
                REPORT['result'] = 'passed'
            finally:
                await host.close()
                REPORT['cleanup_errors'] = [e for a in metrics for e in a.runtime.cleanup_errors]
                assert not REPORT['cleanup_errors'], REPORT['cleanup_errors']


try:
    asyncio.run(main())
except Exception as exc:
    from rockycode.engine.redact import redact
    REPORT['result'] = 'failed'
    REPORT['error'] = redact(str(exc))[:1200]
    raise SystemExit('Live validation failed: ' + REPORT['error']) from None
finally:
    output = ROOT / 'artifacts/runtime-live.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2))
    print('Report: artifacts/runtime-live.json', flush=True)
