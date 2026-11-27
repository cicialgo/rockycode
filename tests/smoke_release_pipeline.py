"""Release permission, tamper, packaging and tag regressions; no publishing."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import re
import shlex
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'scripts/release_check.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def policy(workflow):
    # BaseLoader preserves 'on' as a string and avoids YAML 1.1 bool surprises.
    assert workflow['permissions'] == {}
    assert set(workflow['on']) == {'push', 'pull_request', 'workflow_dispatch'}
    jobs = workflow['jobs']
    assert set(jobs) == {'build', 'pypi'}
    assert jobs['build']['permissions'] == {'contents': 'read'}
    publish = jobs['pypi']
    assert publish['permissions'] == {'id-token': 'write'}
    assert publish['needs'] == 'build' and publish['environment']['name'] == 'pypi'
    assert ' '.join(publish['if'].split()) == (
        "github.repository == 'cicialgo/rockycode' && "
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')")
    assert len(publish['steps']) == 3
    assert publish['steps'][0]['uses'].startswith('actions/download-artifact@')
    assert publish['steps'][0]['with']['artifact-ids'] == '${{ needs.build.outputs.artifact-id }}'
    assert 'assert actual == expected' in publish['steps'][1]['run']
    assert publish['steps'][2]['uses'].startswith('pypa/gh-action-pypi-publish@')
    assert publish['steps'][2]['with']['attestations'] == 'true'
    assert publish['steps'][2]['with']['skip-existing'] == 'false'
    for job in jobs.values():
        for step in job['steps']:
            if 'uses' in step:
                assert re.fullmatch(r'[\w-]+/[\w-]+@[0-9a-f]{40}', step['uses'])
            if step.get('uses', '').startswith('actions/checkout@'):
                assert step['with']['persist-credentials'] == 'false'
    steps = jobs['build']['steps']
    setup = next(s for s in steps if s.get('uses', '').startswith('astral-sh/setup-uv@'))
    assert re.fullmatch(r'\d+\.\d+\.\d+', setup['with']['version'])
    assert re.fullmatch(r'[0-9a-f]{64}', setup['with']['checksum'])
    assert setup['with']['enable-cache'] == 'false'
    commands = '\n'.join(s.get('run', '') for s in steps)
    assert 'uv sync --locked --no-install-project --no-build' in commands
    assert 'git merge-base --is-ancestor HEAD origin/main' in commands
    assert 'scripts/release_check.py --tag "$RELEASE_TAG" --check-only' in commands
    assert 'tests/run_all.py' in commands


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)

    def test_policy(self):
        policy(self.workflow)

    def test_dangerous_policy_mutations_rejected(self):
        mutations = [
            lambda w: w.update(permissions={'id-token': 'write'}),
            lambda w: w['jobs']['build'].update(permissions={'id-token': 'write'}),
            lambda w: w['jobs']['pypi'].update({'if': "startsWith(github.ref, 'refs/tags/v')"}),
            lambda w: w['jobs']['pypi'].pop('needs'),
            lambda w: w['jobs']['pypi']['steps'].insert(0, {'uses': 'actions/checkout@v4'}),
            lambda w: w['jobs']['build']['steps'][0].update(uses='actions/checkout@v7'),
            lambda w: w['jobs']['build']['steps'][0]['with'].update({'persist-credentials': 'true'}),
            lambda w: w['jobs']['pypi']['steps'][2]['with'].update({'skip-existing': 'true'}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                workflow = copy.deepcopy(self.workflow)
                mutate(workflow)
                with self.assertRaises((AssertionError, KeyError)):
                    policy(workflow)

    def test_tag_contract(self):
        project = {'project': {'version': '0.3.0'}}
        release.check_tag('v0.3.0', project, '## [0.3.0] — Protocol update\n')
        for tag, version, changelog in [
            ('main', '0.3.0', '## [0.3.0]'), ('v0.3.1', '0.3.0', '## [0.3.0]'),
            ('v0.3.0rc1', '0.3.0rc1', '## [0.3.0rc1]'),
            ('v0.2.0', '0.2.0', '## [0.2.0]'), ('v0.3.0', '0.3.0', '## [Unreleased]'),
        ]:
            with self.subTest(tag=tag, version=version):
                with self.assertRaises(ValueError):
                    release.check_tag(tag, {'project': {'version': version}}, changelog)

    def test_path_traversal(self):
        for name in ('/etc/passwd', '../secret', 'rockycode/../../secret', 'rockycode\\secret'):
            with self.assertRaises(ValueError):
                release.safe_name(name)

    def test_extra_stale_and_tampered_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            wheel, sdist = path / 'a.whl', path / 'a.tar.gz'
            wheel.write_bytes(b'wheel')
            sdist.write_bytes(b'sdist')
            before = release.hashes(path)
            wheel.write_bytes(b'tampered')
            self.assertNotEqual(before, release.hashes(path))
            (path / 'old.whl').write_bytes(b'stale')
            with self.assertRaises(ValueError):
                release.hashes(path)

    def test_backend_lock_and_sdist_allowlist(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        backend = project['build-system']['requires']
        self.assertEqual(len(backend), 1)
        self.assertRegex(backend[0], r'^hatchling==\d+\.\d+\.\d+$')
        lock = (ROOT / 'scripts/release-requirements.txt').read_text()
        self.assertIn(backend[0], lock)
        requirements = [line for line in lock.replace('\\\n', '').splitlines()
                        if line and not line.lstrip().startswith('#')]
        self.assertGreater(len(requirements), 1)
        for line in requirements:
            self.assertRegex(line, r'^[\w-]+==[\d.]+\s+--hash=sha256:[0-9a-f]{64}')
        self.assertEqual(set(project['tool']['hatch']['build']['targets']['sdist']['only-include']),
                         {'rockycode', 'pyproject.toml', 'README.md', 'LICENSE', 'CHANGELOG.md'})

    # The desktop ships as a source preview (docs/release-runbook.md §5, Stage A);
    # only its internal planning and machine-audit notes stay private.
    PRIVATE_DESKTOP_DOCS = frozenset({
        'apps/desktop/docs/ADR.md', 'apps/desktop/docs/PLAN.md', 'apps/desktop/docs/VALIDATION.md',
        'apps/desktop/docs/POST_MERGE_REGRESSION_20261024.md', 'apps/desktop/docs/SUPPLY_CHAIN.md'})
    SHIPPED_DESKTOP = ('apps/desktop/README.md', 'apps/desktop/README.zh-CN.md', 'apps/desktop/docs/MODEL_RACE.md',
                       'apps/desktop/docs/TASK_UX.md', 'apps/desktop/runtime/rockycode_desktop/host.py',
                       'tests/smoke_desktop_race.py', '.github/workflows/desktop.yml')

    def test_public_export_ships_the_desktop_without_its_private_notes(self):
        for path in self.SHIPPED_DESKTOP:
            self.assertTrue((ROOT / path).exists(), path)
        script = ROOT / 'scripts/public-export.sh'
        if not script.exists():
            # This test also runs in the curated public tree, where the dev-only
            # exporter and the private notes must both already have been removed.
            for path in self.PRIVATE_DESKTOP_DOCS:
                self.assertFalse((ROOT / path).exists(), path)
            return
        line = next(line for line in script.read_text().splitlines() if line.startswith('EXCLUDE='))
        excluded = set(shlex.split(line.removeprefix('EXCLUDE='))[0].split())
        self.assertTrue(self.PRIVATE_DESKTOP_DOCS <= excluded, excluded)
        self.assertFalse({'apps', 'apps/desktop', '.github/workflows/desktop.yml', 'tests/smoke_desktop_race.py'} & excluded)
        for path in self.PRIVATE_DESKTOP_DOCS:  # a renamed note would silently start shipping
            self.assertTrue((ROOT / path).exists(), path)

    def test_public_readmes_link_only_to_shipped_files(self):
        # The root READMEs never link into folders that do not ship; every README's
        # link to a .md file resolves (the exporter re-checks this on the export).
        for name in ('README.md', 'README.zh-CN.md'):
            self.assertIsNone(re.search(r'\]\((docs|tools|experiments)/[^)]+\)', (ROOT / name).read_text()), name)
        for name in ('README.md', 'README.zh-CN.md', 'apps/desktop/README.md', 'apps/desktop/README.zh-CN.md'):
            for link in re.findall(r'\]\(([^)#: ]+\.md)\)', (ROOT / name).read_text()):
                if not link.startswith('/'):
                    self.assertTrue(((ROOT / name).parent / link).exists(), f'{name} → {link}')

if __name__ == '__main__':
    unittest.main()
