"""Offline oracle positive/negative controls on pinned repositories (Docker)."""
import asyncio
import json
from pathlib import Path
import tempfile

from bench.prompt_repo_eval import DEFAULT_FIXTURES, install_cases
from bench import prompt_ablation as lab
from bench.prompt_repo_cases import CASES
from rockycode.engine.sandbox import ChatSandbox


def reference(case, root):
    if case == 'toml_missing':
        p=root/'tomlkit/toml_file.py'
        s=p.read_text().replace('    def read(self) -> TOMLDocument:', '''    def read(self, *, missing_ok=False) -> TOMLDocument:
        try:
            return self._read_existing()
        except FileNotFoundError:
            if missing_ok:
                return TOMLDocument()
            raise

    def _read_existing(self) -> TOMLDocument:''')
        p.write_text(s)
    elif case == 'toml_sort_docs':
        (root/'docs/serialization-policy.md').write_text('Mechanical scope control only; semantic prose needs review.\n')
    elif case == 'vulture_noqa':
        p=root/'vulture/noqa.py'
        s=p.read_text().replace('c.strip() for c in (match.groupdict()["codes"] or "all").split(",")',
            'c.upper() for c in re.split(r"[,\\s]+", match.groupdict()["codes"].strip())\n        if c')
        s=s.replace('    return [\n        c.upper()', '    if not match.groupdict()["codes"]:\n        return ["all"]\n    return [\n        c.upper()')
        p.write_text(s)
    elif case == 'vulture_min_size':
        p=root/'vulture/core.py'
        s=p.read_text()
        s=s.replace('self, min_confidence=0, sort_by_size=False\n', 'self, min_confidence=0, sort_by_size=False, min_size=0\n')
        s=s.replace('        if not 0 <= min_confidence <= 100:',
            '        if type(min_size) is not int or min_size < 0:\n            raise ValueError("min_size must be a nonnegative integer")\n        if not 0 <= min_confidence <= 100:')
        s=s.replace('if obj.confidence >= min_confidence', 'if obj.confidence >= min_confidence and obj.size >= min_size')
        s=s.replace('self, min_confidence=0, sort_by_size=False, make_whitelist=False',
            'self, min_confidence=0, sort_by_size=False, make_whitelist=False, min_size=0')
        s=s.replace('min_confidence=min_confidence, sort_by_size=sort_by_size',
            'min_confidence=min_confidence, sort_by_size=sort_by_size, min_size=min_size')
        s=s.replace('            min_confidence=config["min_confidence"],',
            '            min_size=config["min_size"],\n            min_confidence=config["min_confidence"],')
        p.write_text(s)
        p=root/'vulture/config.py'
        s=p.read_text()
        s=s.replace('    "min_confidence": 0,','    "min_confidence": 0,\n    "min_size": 0,')
        s=s.replace('    if not config["paths"]:',
            '    if type(config["min_size"]) is not int or config["min_size"] < 0:\n        raise InputError("min_size must be a nonnegative integer")\n    if not config["paths"]:')
        s=s.replace('    namespace = parser.parse_args(args)',
            '    parser.add_argument("--min-size", type=int, default=missing)\n    namespace = parser.parse_args(args)')
        p.write_text(s)


async def main():
    install_cases(DEFAULT_FIXTURES)
    records=[]
    for key,case in CASES.items():
        for good in [False,True]:
            with tempfile.TemporaryDirectory(prefix='rocky-repo-oracle-') as tmp:
                root=Path(tmp)
                for name,body in lab.FIXTURES[key].items():
                    p=root/name
                    p.parent.mkdir(parents=True,exist_ok=True)
                    p.write_text(body)
                if good:
                    reference(key,root)
                sandbox=await ChatSandbox.start(root,image='rocky-prompt-repo-eval:20261003',network=False)
                try:
                    output,code=await sandbox.exec('python3 -B -',stdin=case['acceptance'].encode(),timeout=120)
                finally:
                    await sandbox.stop()
                records.append({'case':key,'positive':good,'exit_code':code,'output':output})
                print(key,'positive' if good else 'negative',code,output[-350:],flush=True)
                assert (code==0)==good,(key,good,output)
    (DEFAULT_FIXTURES/'oracle-controls.json').write_text(json.dumps(records,indent=2)+'\n')
    print('REPO ORACLE CONTROLS OK: no model calls')


if __name__=='__main__':
    asyncio.run(main())
