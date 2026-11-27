"""Keep the desktop race's offline safety tests in the existing core CI gate."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'apps' / 'desktop' / 'runtime'))
suite = unittest.defaultTestLoader.discover(str(ROOT / 'apps' / 'desktop' / 'runtime' / 'tests'), pattern='test_race*.py')
result = unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit(1)
print(f'PASS {result.testsRun} offline race tests; no live model calls')
