"""Host messages are catalog keys: the renderer localizes, never matches sentences."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.messages import HostError, error_payload, keyed, text

PACKAGE = Path(__file__).resolve().parents[1] / 'rockycode_desktop'
EN_TOML = Path(__file__).resolve().parents[4] / 'rockycode' / 'i18n' / 'locales' / 'en.toml'
CJK = re.compile(r'[一-鿿]')
try:
    import rockycode.i18n  # noqa: F401 — the shared catalogs; absent in the desktop-only CI
    CORE = True
except ImportError:
    CORE = False


class MessageTests(unittest.TestCase):
    def test_host_error_is_a_keyed_value_error(self):
        error = HostError('diagnostic.sessionMissing')
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.key, 'diagnostic.sessionMissing')
        self.assertEqual(str(error), 'Session not found' if CORE else 'diagnostic.sessionMissing')
        with_params = HostError('race.moreFilesOmitted', count=3)
        self.assertEqual(with_params.params, {'count': 3})
        if CORE:
            self.assertEqual(str(with_params), '3 more files omitted from this preview.')

    def test_error_payload_carries_the_key_and_never_chinese(self):
        payload = error_payload(HostError('diagnostic.workspaceMissing'))
        self.assertEqual(payload['data'], {'key': 'diagnostic.workspaceMissing', 'params': {}})
        self.assertEqual(payload['code'], -32602)
        plain = error_payload(ValueError('Output limit'))
        self.assertEqual((plain['message'], plain['data']['key']), ('Output limit', None))
        internal = error_payload(RuntimeError('stack details stay inside'))
        self.assertEqual(internal['data']['key'], 'diagnostic.requestProcessing')
        self.assertNotIn('stack', internal['message'])
        for value in (payload, plain, internal):
            self.assertIsNone(CJK.search(value['message']), value)

    def test_keyed_persists_text_key_and_params(self):
        record = keyed('race.error.timeLimit')
        self.assertEqual(record['error_key'], 'race.error.timeLimit')
        self.assertEqual(record['error_params'], {})
        self.assertEqual(record['error'], text('race.error.timeLimit'))

    @unittest.skipUnless(EN_TOML.exists(), 'needs the core checkout (apps/desktop lives beside it)')
    def test_every_key_the_host_sends_exists_in_the_catalog(self):
        import tomllib
        catalog = tomllib.loads(EN_TOML.read_text(encoding='utf-8'))['desktop']
        used = {}
        for path in PACKAGE.glob('*.py'):
            for match in re.finditer(r"""['"]((?:diagnostic|race)\.[A-Za-z_.0-9]+)['"]""", path.read_text(encoding='utf-8')):
                used.setdefault(match.group(1), path.name)
        self.assertGreater(len(used), 40, used)
        missing = {}
        for key, where in used.items():
            node = catalog
            for part in key.split('.'):
                node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, str):
                missing[key] = where
        self.assertEqual(missing, {})

    def test_host_source_has_no_user_facing_chinese_outside_fixture_content(self):
        # Fixture replies are sample content, not UI; everything else is a key.
        for path in PACKAGE.glob('*.py'):
            lines = path.read_text(encoding='utf-8').splitlines()
            allowed = set()
            if path.name == 'host.py':
                start = next(i for i, line in enumerate(lines) if line.startswith('    async def fixture_stream'))
                end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('    def ') or lines[i].startswith('    async def '))
                allowed = set(range(start, end))
            for number, line in enumerate(lines):
                if CJK.search(line) and not line.lstrip().startswith('#'):
                    self.assertIn(number, allowed, (path.name, number + 1, line.strip()[:60]))


if __name__ == '__main__':
    unittest.main()
