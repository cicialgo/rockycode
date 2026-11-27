#!/usr/bin/env python3
"""Build-only release rehearsal. Never creates a tag, publishes, or calls a model.

Run from a clean, committed checkout after uv sync --locked --no-install-project.
The build backend and ALL its dependencies are installed from a hash lock into a
fresh temporary venv; builds run offline without PEP 517 dependency resolution.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from email.parser import BytesParser
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def check_tag(tag: str, project: dict, changelog: str) -> None:
    version = project['project']['version']
    if not re.fullmatch(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', tag):
        raise ValueError('release tags must be stable vX.Y.Z versions')
    if tag != f'v{version}':
        raise ValueError('tag does not match pyproject.toml')
    if tuple(map(int, version.split('.'))) <= (0, 2, 0):
        raise ValueError('the new model protocol requires a new core version after 0.2.0')
    if not re.search(rf'^## \[{re.escape(version)}\](?:\s|$)', changelog, re.M):
        raise ValueError('missing versioned CHANGELOG section')


def safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError(f'unsafe distribution member: {name}')


def verify_payload(wheel: Path, sdist: Path, source: Path, version: str) -> None:
    """Check exact packaged core bytes, resource presence, metadata and allowlist."""
    expected = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in (source / 'rockycode').rglob('*') if p.is_file()
    }
    required = {'rockycode/models.toml', 'rockycode/runtime.py',
                'rockycode/engine/loop.py', 'rockycode/modes/learn/learn.md',
                'rockycode/skills/architecture-viz/template.html'}
    if not required <= expected.keys():
        raise ValueError('source tree is missing required core resources')
    dist_info = f'rockycode-{version}.dist-info/'
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate wheel members')
        for name in names:
            safe_name(name)
            if not name.startswith(('rockycode/', dist_info)):
                raise ValueError(f'unexpected wheel member: {name}')
        core = {n: archive.read(n) for n in names if n.startswith('rockycode/')}
        if core != expected:
            raise ValueError('wheel core differs from source (including bundled resources)')
        metadata = BytesParser().parsebytes(archive.read(dist_info + 'METADATA'))
        if (metadata['Name'], metadata['Version']) != ('rockycode', version):
            raise ValueError('wheel metadata/version mismatch')
        if 'rockycode = rockycode.cli:app' not in archive.read(dist_info + 'entry_points.txt').decode():
            raise ValueError('missing CLI entry point')
    with tarfile.open(sdist) as archive:
        core = {}
        seen = set()
        allowed = {'pyproject.toml', 'README.md', 'LICENSE', 'CHANGELOG.md', 'PKG-INFO'}
        for member in archive.getmembers():
            safe_name(member.name)
            prefix = f'rockycode-{version}/'
            if not member.name.startswith(prefix) or not member.isfile():
                raise ValueError(f'unexpected sdist member: {member.name}')
            name = member.name.removeprefix(prefix)
            if name in seen:
                raise ValueError('duplicate sdist members')
            seen.add(name)
            if name not in allowed and not name.startswith('rockycode/'):
                raise ValueError(f'private/unexpected sdist member: {name}')
            data = archive.extractfile(member).read()
            if name.startswith('rockycode/'):
                core[name] = data
            elif name == 'PKG-INFO':
                metadata = BytesParser().parsebytes(data)
                if (metadata['Name'], metadata['Version']) != ('rockycode', version):
                    raise ValueError('sdist metadata/version mismatch')
            elif data != (source / name).read_bytes():
                raise ValueError(f'sdist source differs: {name}')
        if core != expected or not allowed <= seen:
            raise ValueError('sdist is missing source or metadata')


def hashes(directory: Path) -> dict[str, str]:
    files = list(directory.iterdir())
    if len(files) != 2 or any(not p.is_file() or p.is_symlink() for p in files):
        raise ValueError('expected exactly one wheel and one sdist')
    if len(list(directory.glob('*.whl'))) != 1 or len(list(directory.glob('*.tar.gz'))) != 1:
        raise ValueError('expected exactly one wheel and one sdist')
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='also require a stable, version-matched release tag')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    version = project['project']['version']
    if args.tag:
        check_tag(args.tag, project, (ROOT / 'CHANGELOG.md').read_text())
    if args.check_only:
        if not args.tag:
            parser.error('--check-only requires --tag')
        print('release tag/version/changelog: OK')
        return 0
    run('git', 'diff', '--exit-code', 'HEAD', '--')
    commit = run('git', 'rev-parse', 'HEAD')
    epoch = run('git', 'show', '-s', '--format=%ct', 'HEAD')
    env = {**os.environ, 'SOURCE_DATE_EPOCH': epoch, 'PYTHONHASHSEED': '0', 'TZ': 'UTC'}
    lock = ROOT / 'scripts/release-requirements.txt'
    archive_bytes = subprocess.check_output(['git', 'archive', 'HEAD'], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='rocky-release-') as tmp:
        base = Path(tmp)
        build_env = base / 'backend'
        run('uv', 'venv', '--python', sys.executable, str(build_env))
        build_python = build_env / 'bin/python'
        run('uv', 'pip', 'sync', '--python', str(build_python), '--require-hashes',
            '--only-binary=:all:', '--no-config', '--default-index', 'https://pypi.org/simple', str(lock))
        outputs = []
        for index in range(2):
            source, output = base / f'source-{index}', base / f'dist-{index}'
            source.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
                allowed = {'rockycode', 'pyproject.toml', 'README.md', 'LICENSE', 'CHANGELOG.md'}
                members = [m for m in archive.getmembers() if PurePosixPath(m.name).parts[0] in allowed]
                archive.extractall(source, members=members, filter='data')
            # Stop Hatch from discovering an ancestor's .gitignore. No VCS
            # metadata enters the sdist (Hatch force-includes ignore files).
            (source / '.git').mkdir()
            command = ['uv', 'build', str(source), '--python', str(build_python),
                       '--offline', '--no-build-isolation', '--no-config', '--no-create-gitignore',
                       '--out-dir', str(output)]
            # First: direct wheel. Second: wheel from sdist (uv's default).
            if index == 0:
                command += ['--sdist', '--wheel']
            run(*command, cwd=base, env=env)
            verify_payload(next(output.glob('*.whl')), next(output.glob('*.tar.gz')), source, version)
            outputs.append(output)
        first = hashes(outputs[0])
        if first != hashes(outputs[1]):
            raise ValueError('builds differ: direct source vs fresh source/sdist rebuild')
        wheel = next(outputs[1].glob('*.whl'))
        # A clean environment with hashed runtime dependencies from uv.lock.
        # -I and an external cwd prove that tests import this wheel, not the checkout.
        install_env = base / 'installed'
        run('uv', 'venv', '--python', sys.executable, str(install_env))
        installed_python = install_env / 'bin/python'
        runtime_lock = base / 'runtime-requirements.txt'
        run('uv', 'export', '--locked', '--no-dev', '--no-emit-project',
            '--format', 'requirements-txt', '--output-file', str(runtime_lock))
        run('uv', 'pip', 'sync', '--python', str(installed_python), '--require-hashes',
            '--only-binary=:all:', '--no-config', '--default-index', 'https://pypi.org/simple',
            str(runtime_lock))
        run('uv', 'pip', 'install', '--python', str(installed_python), '--no-deps', '--offline', str(wheel))
        smoke_env = {**env, 'ROCKYCODE_HOME': str(base / 'home'),
                     'RELEASE_INSTALLED_ROOT': str(install_env), 'RELEASE_EXPECTED_VERSION': version}
        for test in ('smoke_release_models.py', 'smoke_model_protocol.py', 'smoke_auxiliary.py'):
            result = run(str(installed_python), '-I', str(ROOT / 'tests' / test),
                         cwd=base, env=smoke_env)
            print(result)
        run(str(install_env / 'bin/rockycode'), '--version', cwd=base, env=smoke_env)
        # Fail rather than mixing stale/unreviewed artifacts into a later upload.
        target = ROOT / 'dist'
        if target.exists() and any(target.iterdir()):
            raise ValueError('dist is not empty; use a clean checkout or move old artifacts first')
        target.mkdir(exist_ok=True)
        for path in outputs[1].iterdir():
            shutil.copyfile(path, target / path.name)
        evidence = {'source_commit': commit, 'source_date_epoch': epoch,
                    'version': version, 'python': sys.version, 'uv': run('uv', '--version'),
                    'build_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
                    'distributions': first, 'direct_and_sdist_rebuild_match': True,
                    'installed_wheel_models': 'passed', 'live_models': 'not run'}
        (ROOT / 'build').mkdir(exist_ok=True)
        (ROOT / 'build/release-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('hashes=' + json.dumps(first) + '\n')
        print(json.dumps(evidence, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or '', file=sys.stderr)
        print(exc.stderr or '', file=sys.stderr)
        raise
