"""Accuracy factors stay isolated; hidden oracles accept correct code and reject regressions."""
import subprocess
import sys
import tempfile
from pathlib import Path

from bench.prompt_ablation import variants
from bench.prompt_accuracy_cases import ACCURACY_ARMS, ACCURACY_RULES, CASES, FIXTURES

for lang in ('en', 'zh'):
    arms = variants(lang)
    control = arms['adapted'].split('\n\n', 3)
    for name, factors in ACCURACY_ARMS.items():
        parts = arms[name].split('\n\n', 3)
        assert [parts[i] for i in (0, 1, 3)] == [control[i] for i in (0, 1, 3)]
        for factor in ACCURACY_RULES:
            assert (ACCURACY_RULES[factor][lang] in parts[2]) == (factor in factors)

solutions = {
    'interval': {'app.py': 'def overlaps(a,b): return max(a[0],b[0]) <= min(a[1],b[1])\n'},
    'lookup_contract': {'app.py': '_missing=object()\ndef lookup(mapping,key,default=_missing):\n    if default is _missing: return mapping[key]\n    return mapping.get(key,default)\n'},
    'config_layers': {'app.py': 'def resolve_timeout(cli,env,config): return next((v for v in (cli,env,config) if v is not None),None)\n'},
    'lazy_parse': {'app.py': 'def parse(values):\n    try: return [int(x) for x in values]\n    except ValueError: return []\n'},
    'sql_counts': {'query.sql': "SELECT c.id, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id=c.id AND o.status='paid' GROUP BY c.id ORDER BY c.id;\n"},
    'stable_ranking': {'app.py': FIXTURES['stable_ranking']['app.py'].replace('def rank(rows):','def rank(rows, limit=None):').replace('reverse=True)','reverse=True)[:limit]')},
    'retry_docs': {'README.md': 'retries counts additional attempts; maximum retries+1. Success returns early. ValueError retries; TypeError propagates.\n'},
    'copy_state': {'app.py': 'from copy import deepcopy\ndef draft(record): return deepcopy(record)\n'},
}
for name, case in CASES.items():
    assert len(case['en']) == len(case['zh']) == len(case['rubric']) == 3
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for path, content in FIXTURES[name].items():
            (root / path).write_text(content)
        bad = subprocess.run([sys.executable, '-B', '-c', case['acceptance']], cwd=root, capture_output=True)
        assert bad.returncode != 0, f'{name}: unchanged code must not satisfy the new requirement'
        for path, content in solutions[name].items():
            (root / path).write_text(content)
        good = subprocess.run([sys.executable, '-B', '-c', case['acceptance']], cwd=root, capture_output=True)
        assert good.returncode == 0, (name, good.stderr.decode())
print('PROMPT ACCURACY SMOKE OK — factors isolated, eight oracles accept solutions and reject unmet requirements')
