"""Bounded 2x2 prompt experiment using the real Engine and ChatSandbox.

Run as `python3 -m bench.prompt_ablation --dry-run` first. Live execution is
explicit (--live); no new images are pulled, no user checkout is mounted, and
no generated code executes on the host. This is a development pilot, not a
claim of statistically established improvement or an interactive-chat replica.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import shlex
import shutil
import subprocess
import tempfile
import time
import types
import uuid
from pathlib import Path

from bench.prompt_cases import CASES, FIXTURES
from bench.prompt_budget import Meter, atomic_json
from bench.prompt_accuracy_cases import (
    CASES as ACCURACY_CASES, FIXTURES as ACCURACY_FIXTURES, ACCURACY_ARMS, ACCURACY_RULES,
)

from rockycode.prompts.rocky import ROCKY_SYSTEM, ROCKY_SYSTEM_ZH, tools_section, with_language

TASKFIT_CASES = list(CASES)
CASES = {**CASES, **ACCURACY_CASES}
FIXTURES = {**FIXTURES, **ACCURACY_FIXTURES}

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).with_name("prompt_baseline_278a9dd.json")
ARM_FACTORS = {"baseline": (0, 0), "scope_only": (1, 0),
               "completion_only": (0, 1), "adapted": (1, 1)}
ALL_ARMS = {**ARM_FACTORS, **ACCURACY_ARMS}


def sha(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def variants(language: str) -> dict[str, str]:
    old = json.loads(BASELINE_PATH.read_text())[language].split("\n\n", 3)
    new = (ROCKY_SYSTEM_ZH if language == "zh" else ROCKY_SYSTEM).split("\n\n", 3)
    if len(old) != 4 or len(new) != 4 or old[0] != new[0] or old[3] != new[3]:
        raise ValueError("prompt structure or Rocky's identity/style changed; review the experiment")
    result = {name: "\n\n".join([old[0], (new if scope else old)[1],
                               (new if completion else old)[2], old[3]])
            for name, (scope, completion) in ARM_FACTORS.items()}
    for name, factors in ACCURACY_ARMS.items():
        parts = list(new)
        if factors:
            parts[2] += "\n" + "\n".join(ACCURACY_RULES[f][language] for f in factors)
        result[name] = "\n\n".join(parts)
    return result


def schedule(cases, languages, arms, reps: int, seed: int) -> list[dict]:
    """Pair within a scenario/language/rep, rotating order across blocks."""
    rng = random.Random(seed)
    result = []
    for rep in range(1, reps + 1):
        blocks = [(case, lang) for case in cases for lang in languages]
        rng.shuffle(blocks)
        for case, lang in blocks:
            order = list(arms)
            rng.shuffle(order)
            result.extend(dict(case=case, language=lang, arm=arm, rep=rep) for arm in order)
    return result


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if any(part in ('__pycache__', '.pytest_cache') for part in path.parts):
            continue
        if path.is_symlink():
            out[str(path.relative_to(root))] = "symlink:" + os.readlink(path)
        elif path.is_file():
            if path.stat().st_size > 256_000 or len(out) > 100:
                raise RuntimeError("fixture grew beyond pilot limits")
            out[str(path.relative_to(root))] = path.read_bytes().decode('utf-8', errors='replace')
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def write_json(path: Path, value) -> None:
    atomic_json(path, value)


def capture_workspace(root: Path) -> dict:
    import base64
    files = {}
    for path in sorted(root.rglob('*')):
        if any(part in ('__pycache__', '.pytest_cache') for part in path.parts):
            continue
        if path.is_symlink():
            raise ValueError('cannot checkpoint symlinks in an experimental fixture')
        if path.is_file():
            if path.stat().st_size > 2_000_000 or len(files) >= 100:
                raise ValueError('checkpoint exceeds fixture limits')
            files[str(path.relative_to(root))] = {
                'data': base64.b64encode(path.read_bytes()).decode(), 'mode': path.stat().st_mode & 0o777}
    return files


def restore_workspace(root: Path, files: dict) -> None:
    import base64
    for name, entry in files.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or Path(name).is_absolute():
            raise ValueError('unsafe checkpoint path')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(entry['data'], validate=True))
        path.chmod(entry['mode'] & 0o777)


async def episode(spec: dict, base: str, args, meter: Meter, image: str, out: Path, resolved) -> dict:
    from rockycode.engine.events import EngineError, TextDelta, ToolStarted, TurnFinished
    from rockycode.engine.loop import Engine
    from rockycode.engine.sandbox import ChatSandbox, build_sandbox_registry

    case = CASES[spec['case']]
    checkpoint_path = out / 'checkpoint.json'
    saved = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else None
    row = saved['row'] if saved else {**spec, 'turns': [], 'acceptance': None, 'error': None}
    row.update(status='running', error=None)
    with tempfile.TemporaryDirectory(prefix='rocky-prompt-fixture-') as temp:
        workdir = Path(temp) / 'workspace'
        workdir.mkdir()
        if saved:
            restore_workspace(workdir, saved['files'])
        else:
            for name, body in FIXTURES[spec['case']].items():
                path = workdir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            meter.episode_start = meter.tokens
        initial = FIXTURES[spec['case']]
        sandbox = await ChatSandbox.start(workdir, image=image, network=False)
        try:
            registry = build_sandbox_registry(sandbox)
            system = with_language(base + tools_section(registry), spec['language'])
            system += '\n\n' + case.get('environment', 'Environment: Linux; bash; repository workdir /workspace; python3 and unittest available.')
            engine_steps = getattr(args, 'engine_max_steps', 0)
            fingerprint = sha({'system': system, 'tools': [t.schema for t in registry.values()],
                               'image': image, 'model': resolved[2], 'effort': args.effort,
                               'max_output': args.max_output, 'engine_max_steps': engine_steps})
            if saved and saved['fingerprint'] != fingerprint:
                raise ValueError('checkpoint model/prompt/tools/image mismatch')
            write_json(out / 'prompt.json', {'base': base, 'effective': system,
                       'base_sha256': sha(base), 'effective_sha256': sha(system),
                       'tools': [t.schema for t in registry.values()]})
            engine = Engine(model=resolved[2], client=types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=meter)),
                system_prompt=system, registry=registry, workdir=workdir,
                max_steps=engine_steps, max_tokens=args.max_output, context_window=65536,
                thinking=True, reasoning_effort=args.effort,
                trajectory_meta={'runner': 'prompt-ablation', **spec})
            engine.adopt(*resolved)
            row.setdefault('trajectories', []).append(str(engine.trajectory.path))
            row['trajectory'] = str(engine.trajectory.path)
            if saved:
                engine.history = saved['history']
                engine._last_prompt_tokens = saved['last_prompt_tokens']
                engine._sent_until = saved['sent_until']
            for turn, message in enumerate(case[spec['language']], 1):
                if turn <= len(row['turns']):
                    continue
                resuming = bool(saved and saved['turn'] == turn and saved['partial'])
                partial = saved['partial'] if resuming else {
                    'before': snapshot(workdir), 'answer': [], 'calls': [], 'usage_before': dict(meter.usage),
                    'elapsed_seconds': 0.0}
                elapsed_before = partial.get('elapsed_seconds', 0.0)
                active_started = time.monotonic()
                if not resuming:
                    meter.turn_calls = 0
                errors = []
                finished = False
                meter.request_context = {**spec, 'turn': turn}

                def save_checkpoint():
                    if partial is not None:
                        partial['elapsed_seconds'] = elapsed_before + time.monotonic() - active_started
                    write_json(checkpoint_path, {'fingerprint': fingerprint, 'row': row,
                        'turn': turn, 'partial': partial, 'history': engine.history,
                        'files': capture_workspace(workdir), 'last_prompt_tokens': engine._last_prompt_tokens,
                        'sent_until': engine._sent_until, 'calls': meter.calls, 'tokens': meter.tokens,
                        'loop_state': engine.export_turn_state()})

                meter.before_request = save_checkpoint

                async def drain():
                    nonlocal finished
                    stream = engine.resume_turn(state=saved.get('loop_state')) if resuming else engine.run_turn(message)
                    async for ev in stream:
                        if isinstance(ev, TextDelta):
                            partial['answer'].append(ev.text)
                        elif isinstance(ev, ToolStarted):
                            partial['calls'].append({'tool': ev.tool, 'args': ev.args})
                        elif isinstance(ev, EngineError):
                            errors.append(ev.message)
                        elif isinstance(ev, TurnFinished):
                            finished = True
                try:
                    await asyncio.wait_for(drain(), timeout=max(0, args.turn_timeout - elapsed_before))
                except Exception as exc:
                    errors.append(f'{type(exc).__name__}: {exc}')
                after = snapshot(workdir)
                write_json(out / f'files-turn-{turn}.json', after)
                usage = {k: meter.usage[k] - partial['usage_before'].get(k, 0) for k in meter.usage}
                writes = [c for c in partial['calls'] if c['tool'] in ('write_file', 'edit_file')]
                delta = changed(partial['before'], after)
                read_only = turn not in case['write_turns']
                turn_row = {'turn': turn, 'user': message, 'answer': ''.join(partial['answer']),
                    'read_only': read_only, 'changed': delta, 'file_write_attempts': len(writes),
                    'read_only_pass': not (read_only and (delta or writes)),
                    'calls': partial['calls'], 'usage': usage, 'errors': errors,
                    'elapsed_seconds': elapsed_before + time.monotonic() - active_started}
                if errors or not finished:
                    row['error'] = errors or ['turn ended without completion']
                    row['status'] = ('budget_exhausted' if errors and all('BudgetStop:' in e for e in errors)
                                     else 'error')
                    row['partial_turn'] = turn_row
                    # The request-boundary checkpoint intentionally precedes errors.
                    # An uncertain API/tool state cannot be automatically resumed.
                    break
                row['turns'].append(turn_row)
                row.pop('partial_turn', None)
                # Save a completed-turn checkpoint before beginning the next turn.
                partial = None
                save_checkpoint()
                meter.checkpoint()
                saved = None
            engine.finalize_outcome()
            row['final_changed'] = changed(initial, snapshot(workdir))
        finally:
            meter.before_request = None
            await sandbox.stop()
        if row['error'] is None:
            row['status'] = 'completed'
            if case['acceptance']:
                verify_dir = Path(temp) / 'verify'
                shutil.copytree(workdir, verify_dir, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
                verifier = await ChatSandbox.start(verify_dir, image=image, network=False)
                try:
                    output, code = await verifier.exec('python3 -B -', stdin=case['acceptance'].encode(),
                        timeout=case.get('acceptance_timeout', 20))
                    row['acceptance'] = {'passed': code == 0, 'output': output, 'exit_code': code}
                finally:
                    await verifier.stop()
    row['mechanical_pass'] = (row['status'] == 'completed'
        and all(t['read_only_pass'] and t['answer'].strip() for t in row['turns'])
        and (row['acceptance'] is None or row['acceptance']['passed']))
    return row


def summarize(rows: list[dict]) -> dict:
    result = {}
    for arm in dict.fromkeys(r["arm"] for r in rows):
        group = [r for r in rows if r["arm"] == arm]
        ro = [t for r in group for t in r["turns"] + ([r["partial_turn"]] if r.get("partial_turn") else []) if t["read_only"]]
        acceptance = [r["acceptance"] for r in group if r["acceptance"] is not None]
        result[arm] = {"episodes": len(group), "completed": sum(completed(r) for r in group),
            "incomplete": sum(not completed(r) for r in group), "mechanical_pass": sum(r["mechanical_pass"] for r in group),
            "read_only_turns": len(ro), "read_only_pass": sum(t["read_only_pass"] for t in ro),
            "acceptance_runs": len(acceptance), "acceptance_pass": sum(a["passed"] for a in acceptance),
            "errors": sum(r.get("status", "error") == "error" and r["error"] is not None for r in group),
            "budget_exhausted": sum(r.get("status") == "budget_exhausted" for r in group),
            "tokens": sum((t["usage"].get("prompt_tokens", 0) or 0)
                          + (t["usage"].get("completion_tokens", 0) or 0) for r in group for t in r["turns"] + ([r["partial_turn"]] if r.get("partial_turn") else []))}
    return result


async def rescore(directory: Path) -> None:
    """Re-evaluate saved final code, preserving original results; no model calls."""
    from rockycode.engine.sandbox import ChatSandbox

    manifest = json.loads((directory / "manifest.json").read_text())
    rows = json.loads((directory / "results.json").read_text())
    for i, row in enumerate(rows, 1):
        case = CASES[row["case"]]
        if not case["acceptance"] or row["error"]:
            continue
        dest = directory / f"{i:03d}-{row['case']}-{row['language']}-{row['arm']}-r{row['rep']}"
        files = json.loads((dest / f"files-turn-{len(row['turns'])}.json").read_text())
        with tempfile.TemporaryDirectory(prefix="rocky-prompt-rescore-") as temp:
            root = Path(temp)
            for name, text in files.items():
                target = root / name
                if not target.resolve().is_relative_to(root.resolve()) or text.startswith("symlink:"):
                    raise ValueError("cannot safely reconstruct this saved fixture")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            verifier = await ChatSandbox.start(root, image=manifest["image"], network=False)
            try:
                output, code = await verifier.exec("python3 -B -c " + shlex.quote(case["acceptance"]), timeout=20)
                row["acceptance"] = {"passed": code == 0, "output": output, "exit_code": code}
            finally:
                await verifier.stop()
        row["mechanical_pass"] = (len(row["turns"]) == len(case[row["language"]])
            and all(t["read_only_pass"] and t["answer"].strip() for t in row["turns"])
            and row["acceptance"]["passed"])
    write_json(directory / "rescored-results.json", rows)
    write_json(directory / "rescored-summary.json", {"arms": summarize(rows),
        "scorer_sha256": sha((ROOT / "bench/prompt_cases.py").read_text()),
        "note": "Rechecked saved code in fresh containers. Original results preserved; zero model calls."})
    print(json.dumps(summarize(rows), indent=2))


def completed(row: dict) -> bool:
    return row.get('status', 'completed' if not row.get('error') else 'error') == 'completed'


def episode_path(out: Path, i: int, spec: dict) -> Path:
    return out / f"{i:03d}-{spec['case']}-{spec['language']}-{spec['arm']}-r{spec['rep']}"


def block_key(spec: dict) -> tuple:
    return spec['case'], spec['language'], spec['rep']


def write_review(out: Path, rows: list, seed: int) -> None:
    shuffled = list(rows)
    random.Random(seed + 1).shuffle(shuffled)
    blind, key = [], {}
    for i, row in enumerate(shuffled, 1):
        bid = f'sample-{i:03d}'
        key[bid] = {k: row[k] for k in ('arm', 'case', 'language', 'rep')}
        turns = row['turns'] + ([row['partial_turn']] if row.get('partial_turn') else [])
        blind.append({'id': bid, 'case': row['case'], 'language': row['language'],
            'status': row.get('status'), 'initial_files': FIXTURES[row['case']],
            'rubric': CASES[row['case']].get('rubric'),
            'conversation': [{'user': t['user'], 'answer': t['answer'], 'changed_files': t['changed'],
                              'tool_calls': t['calls'], 'errors': t['errors']} for t in turns]})
    write_json(out / 'blind-review.json', blind)
    write_json(out / 'blind-key.json', key)


async def live(args, plan: list[dict], out: Path) -> None:
    from openai import AsyncOpenAI
    from rockycode.config import load
    from rockycode.engine.providers import resolve
    from rockycode.onboarding import bootstrap_credentials

    bootstrap_credentials()
    model = args.model or os.getenv('ROCKYCODE_MODEL') or load().get('model')
    resolved = resolve(model) if model else None
    if resolved is None or not resolved[1].key():
        raise RuntimeError('choose a configured model in Rocky\'s provider registry')
    image = subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True).strip()
    sources = ('rockycode/prompts/rocky.py', 'rockycode/engine/loop.py', 'rockycode/engine/tools.py',
               'rockycode/engine/sandbox.py', 'bench/prompt_ablation.py', 'bench/prompt_cases.py',
               'bench/prompt_budget.py', 'bench/prompt_accuracy_cases.py',
               'rockycode/engine/providers.py', 'rockycode/engine/effort.py',
               'rockycode/engine/compaction.py', 'rockycode/session.py', 'rockycode/models.toml')
    sources += tuple(getattr(args, 'extra_sources', ()))
    identity = {'image': image, 'model': resolved[2], 'provider': resolved[0].name,
                'endpoint': resolved[1].base_url, 'source_hashes': {p: sha((ROOT / p).read_text()) for p in sources}}
    if getattr(args, 'fixture_identity', None):
        identity['fixture_identity'] = args.fixture_identity
    old = json.loads((out / 'manifest.json').read_text()) if args.resume else None
    if old:
        if old['schema'] != 2 or any(old[k] != v for k, v in identity.items()):
            raise ValueError('resume requires identical sources, model, endpoint and image')
        # A crash after checkpoint but before ledger commit must not replay state.
        ledger = json.loads((out / 'ledger.json').read_text())
        checkpoints = [json.loads(p.read_text()) for p in out.glob('*/checkpoint.json')]
        if (max((c['calls'] for c in checkpoints), default=0) != ledger['calls']
                or max((c['tokens'] for c in checkpoints), default=0) != ledger['tokens']):
            raise ValueError('checkpoint/ledger request mismatch; reconcile before resume')
    os.environ['ROCKYCODE_HOME'] = str(out / 'runtime')
    manifest = old or {'schema': 2, 'plan': plan, 'arguments': vars(args), **identity,
        'sampling': {'temperature': 'provider default', 'top_p': 'provider default', 'seed': None},
        'engine_max_steps': getattr(args, 'engine_max_steps', 0), 'context_window': 65536, 'network': False,
        'memory': False, 'skills': False, 'explore': False, 'project_instructions': False,
        'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'git_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        'caveat': 'development experiment; mechanical checks do not grade semantic accuracy',
        'budget_history': []}
    # Validate restoration before touching any saved result or metadata. An
    # unsafe/unknown-usage resume must leave the original evidence intact.
    client = AsyncOpenAI(api_key=resolved[1].key(), base_url=resolved[1].base_url, max_retries=0, timeout=90)
    try:
        meter = Meter(client, out, token_cap=args.token_cap, call_cap=args.call_cap,
                      turn_cap=args.turn_call_cap, episode_cap=args.episode_token_cap, restore=bool(old))
    except BaseException:
        await client.close()
        raise
    manifest['budget_history'].append({k: getattr(args, k) for k in ('token_cap', 'call_cap', 'episode_token_cap')})
    write_json(out / 'manifest.json', manifest)
    if not old:
        for name in sources:
            saved = out / 'source' / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text((ROOT / name).read_text())
        write_json(out / 'baseline.json', json.loads(BASELINE_PATH.read_text()))
    rows = []
    stop = ''
    started = time.monotonic()
    try:
        for i, spec in enumerate(plan, 1):
            dest = episode_path(out, i, spec)
            previous = json.loads((dest / 'result.json').read_text()) if (dest / 'result.json').exists() else None
            if previous and completed(previous):
                rows.append(previous)
                continue
            if previous and previous.get('status') != 'budget_exhausted':
                stop = 'non-budget error requires reconciliation; no automatic replay'
                rows.append(previous)
                break
            # Admit an entire matched block with equal per-episode room. A small
            # global cap pauses BEFORE the block, never only before its last arm.
            remaining_block = []
            for j in range(i - 1, len(plan)):
                if block_key(plan[j]) != block_key(spec):
                    break
                remaining_block.append(plan[j])
            used_episode = meter.tokens - meter.episode_start if (dest / 'checkpoint.json').exists() else 0
            needed = len(remaining_block) * args.episode_token_cap - used_episode
            needed_calls = len(remaining_block) * len(CASES[spec['case']][spec['language']]) * args.turn_call_cap
            if meter.tokens + needed > args.token_cap or meter.calls + needed_calls > args.call_cap:
                stop = 'budget reserved for paired block is unavailable; paused before next request'
                if previous:
                    rows.append(previous)
                break
            dest.mkdir(exist_ok=True)
            row = await episode(spec, variants(spec['language'])[spec['arm']], args, meter, image, dest, resolved)
            rows.append(row)
            write_json(dest / 'result.json', row)
            write_json(out / 'results.json', rows)
            print(json.dumps({'progress': f'{i}/{len(plan)}', **spec, 'status': row['status'],
                              'mechanical_pass': row['mechanical_pass'], 'tokens': meter.tokens,
                              'api_calls': meter.calls}, ensure_ascii=False), flush=True)
            if row['status'] != 'completed' or meter.stop_reason:
                stop = meter.stop_reason or row['status']
                break
    finally:
        await client.close()
        write_json(out / 'results.json', rows)
        if 'meter' in locals():
            meter.save()
            write_json(out / 'summary.json', {'planned': len(plan), 'attempted': len(rows),
                'completed': sum(completed(r) for r in rows), 'not_started': len(plan) - len(rows),
                'incomplete': sum(not completed(r) for r in rows), 'arms': summarize(rows),
                'api_calls': meter.calls, 'usage': meter.usage, 'stop_reason': stop or meter.stop_reason,
                'elapsed_seconds_this_invocation': round(time.monotonic() - started, 1)})
        write_review(out, rows, args.order_seed)


def run_live(args, plan, out):
    # Avoid two resumes spending against the same ledger concurrently. The OS
    # releases this advisory lock even when a process crashes.
    import fcntl
    with (out / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(live(args, plan, out))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="make billable API calls in disposable Docker fixtures")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", type=Path, help="recheck saved final code in Docker, without any model calls")
    ap.add_argument("--resume", help="resume a v2 run at a clean boundary; caps are cumulative")
    ap.add_argument("--suite", choices=["taskfit", "accuracy"], default="taskfit")
    ap.add_argument("--model")
    ap.add_argument("--image", default="python:3.12-slim")
    ap.add_argument("--cases", nargs="+", choices=list(CASES), default=None)
    ap.add_argument("--languages", nargs="+", choices=["en", "zh"], default=["en", "zh"])
    ap.add_argument("--arms", nargs="+", choices=list(ALL_ARMS), default=None)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--order-seed", type=int, default=20261003)
    ap.add_argument("--max-output", type=int, default=8192)
    ap.add_argument("--effort", choices=["low", "high", "max"], default="low")
    ap.add_argument("--token-cap", type=int, default=3_000_000)
    ap.add_argument("--episode-token-cap", type=int, default=200_000)
    ap.add_argument("--call-cap", type=int, default=10_000)
    ap.add_argument("--turn-call-cap", type=int, default=40)
    ap.add_argument("--turn-timeout", type=int, default=600)
    ap.add_argument("--output", help="new output directory; existing paths are rejected")
    args = ap.parse_args()
    if args.resume:
        if args.rescore or args.output or not args.live or args.dry_run:
            ap.error("--resume requires --live; cannot combine with --rescore/--output/--dry-run")
        out = Path(args.resume).resolve()
        saved = json.loads((out / "manifest.json").read_text())
        if saved.get("schema") != 2:
            ap.error("legacy pilot runs have no resumable checkpoint")
        # Restore the exact treatment; only cumulative caps may change explicitly.
        import sys
        updates = {k: getattr(args, k) for k in ("token_cap", "call_cap", "episode_token_cap")
                   if any(v == "--" + k.replace("_", "-") or v.startswith("--" + k.replace("_", "-") + "=")
                          for v in sys.argv[1:])}
        args = argparse.Namespace(**saved["arguments"])
        args.resume = str(out)
        for k, v in updates.items():
            setattr(args, k, v)
        if any(getattr(args, k) <= 0 for k in updates):
            ap.error("resume caps must be positive")
        run_live(args, saved["plan"], out)
        return
    if args.rescore:
        if args.live or args.dry_run:
            ap.error("--rescore cannot be combined with --live/--dry-run")
        asyncio.run(rescore(args.rescore.resolve()))
        return
    args.cases = args.cases or (list(ACCURACY_CASES) if args.suite == "accuracy" else TASKFIT_CASES)
    args.arms = args.arms or (list(ACCURACY_ARMS) if args.suite == "accuracy" else list(ARM_FACTORS))
    if any(len(getattr(args, k)) != len(set(getattr(args, k))) for k in ("cases", "languages", "arms")):
        ap.error("cases, languages and arms must be unique; use --reps for repetition")
    if any(getattr(args, x) <= 0 for x in ("reps", "max_output", "token_cap", "episode_token_cap", "call_cap", "turn_call_cap", "turn_timeout")):
        ap.error("all limits and reps must be positive")
    plan = schedule(args.cases, args.languages, args.arms, args.reps, args.order_seed)
    print(json.dumps({"episodes": len(plan), "turns": len(plan) * 3,
        "live": args.live and not args.dry_run, "token_cap": args.token_cap,
        "episode_token_cap": args.episode_token_cap, "max_output": args.max_output,
        "paired_block_reserve": len(args.arms) * args.episode_token_cap,
        "arms": {lang: {a: sha(v) for a, v in variants(lang).items() if a in args.arms}
                 for lang in args.languages}}, indent=2))
    if not args.live or args.dry_run:
        return
    out = Path(args.output or ROOT / "results" / "runs" / ("prompt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    out.mkdir(parents=True, exist_ok=False)
    print("artifacts:", out, flush=True)
    run_live(args, plan, out)


if __name__ == "__main__":
    main()
