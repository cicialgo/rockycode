"""Run frozen prompt candidates on fresh requests against pinned upstream source.

Fixtures are exported once from existing local images. No downloads, source
mutations, benchmark issue texts or reference patches are used as actor inputs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench import prompt_ablation as lab
from bench.prompt_repo_cases import CASES, load_fixtures

DEFAULT_FIXTURES = lab.ROOT / 'results/runs/repo-transfer-fixtures-20261003'
ARMS = ['adapted', 'evidence_only', 'counterexample_only', 'grounded']


def install_cases(root: Path) -> dict:
    fixtures, identity = load_fixtures(root)
    lab.CASES.update(CASES)
    lab.FIXTURES.update(fixtures)
    return identity


def resume_config(manifest: dict, directory: Path, *, token_cap: int | None = None,
                  episode_token_cap: int | None = None) -> argparse.Namespace:
    """Keep the last accepted limits and immutable runtime unless overridden."""
    config = argparse.Namespace(**manifest['arguments'])
    config.resume = str(directory)
    config.image = manifest['image']
    history = manifest.get('budget_history') or []
    if history:
        for name in ('token_cap', 'episode_token_cap', 'call_cap'):
            if name in history[-1]:
                setattr(config, name, history[-1][name])
    for name, value in (('token_cap', token_cap), ('episode_token_cap', episode_token_cap)):
        if value is not None:
            setattr(config, name, value)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--fixtures', type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument('--image', default='rocky-prompt-repo-eval:20261003')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', type=Path, help='Continue a clean checkpoint with the last accepted limits')
    parser.add_argument('--token-cap', type=int, help='Total input/output token cap: new runs 24000000; resume keeps last accepted cap')
    parser.add_argument('--episode-token-cap', type=int, help='Per-conversation token cap: new runs 3000000; resume keeps last accepted cap')
    args = parser.parse_args()
    if any(v is not None and v <= 0 for v in (args.token_cap, args.episode_token_cap)):
        parser.error('token limits must be positive')
    identity = install_cases(args.fixtures.resolve())
    public_proof = json.loads((args.fixtures / 'public-source-proof.json').read_text())
    for repo, spec in identity.items():
        proof = public_proof[repo]
        if (proof['public_commit'] != spec['commit'] or proof['verified_blob_count'] != spec['files']
                or proof['all_archive_bytes_match_public_git_blobs'] is not True):
            raise ValueError('repository public-source proof does not match frozen fixture')
        spec['public_source_proof'] = proof
    frozen = json.loads((args.fixtures / 'frozen-prompts.json').read_text())
    for arm in ARMS:
        if lab.variants('zh')[arm] != frozen['zh'][arm]['text']:
            raise ValueError('candidate changed since repository-task design began')
    baseline = json.loads((args.fixtures / 'baseline-tests.json').read_text())
    if any(x['exit_code'] != 0 for x in baseline.values()):
        raise ValueError('both original repository regression suites must pass first')
    if args.resume:
        if not args.live or args.output:
            parser.error('--resume requires --live and cannot combine with --output')
        out = args.resume.resolve()
        manifest = json.loads((out / 'manifest.json').read_text())
        config = resume_config(manifest, out, token_cap=args.token_cap,
                               episode_token_cap=args.episode_token_cap)
        lab.run_live(config, manifest['plan'], out)
        return
    config = argparse.Namespace(model=None, image=args.image, languages=['zh'], arms=ARMS,
        cases=list(CASES), reps=1, order_seed=20261004, max_output=16384, effort='high',
        token_cap=args.token_cap or 24_000_000, episode_token_cap=args.episode_token_cap or 3_000_000, call_cap=10000,
        turn_call_cap=120, turn_timeout=1200, engine_max_steps=100, resume=None,
        fixture_identity=identity, extra_sources=['bench/prompt_repo_cases.py','bench/prompt_repo_eval.py'],
        live=args.live)
    plan = lab.schedule(config.cases,config.languages,config.arms,config.reps,config.order_seed)
    print(json.dumps({'conversations':len(plan),'user_turns':len(plan)*3,'engine_steps':'100+10',
        'effort':config.effort,'max_output':config.max_output,'token_cap':config.token_cap,
        'fixtures':identity,'prompt_hashes':{a:lab.sha(lab.variants('zh')[a]) for a in ARMS}},indent=2))
    if not args.live:
        return
    if args.output is None:
        parser.error('--live requires a new --output directory')
    out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    # Keep the complete exact corpus and its provenance next to each run.
    lab.write_json(out/'repo-fixtures.json',{k:lab.FIXTURES[k] for k in CASES})
    lab.write_json(out/'repo-cases.json',CASES)
    lab.write_json(out/'baseline-tests.json',baseline)
    lab.write_json(out/'public-source-proof.json',public_proof)
    lab.run_live(config,plan,out)


if __name__=='__main__':
    main()
