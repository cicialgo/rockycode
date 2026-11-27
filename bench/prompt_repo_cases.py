"""Fresh requests on immutable upstream repositories; no benchmark issue/patch reuse.

All requirements and executable acceptance are frozen before live sampling.
Acceptance runs only in a second container and never enters actor context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bench.prompt_accuracy_cases import case

REPOS = {
    'tomlkit': {'commit': 'dd05eebc8ed9e30fc6c223088a5a450cb54c1cab',
        'archive_sha256': '46c57c649d6b63e5b1bef436e3f250c0fe3ebf0723fa3d686e8aaf48fefe6d1e',
        'source_image': 'sha256:76b6264ad880f48a8f71916ac36bb11049a0ebaa1ae6f755c130bd44f1245595',
        'regression': ['--ignore=tests/test_toml_tests.py']},
    'vulture': {'commit': '1eb212f0a0707ad6f4c720bb2010c2b7517cf0f9',
        'archive_sha256': 'b259226aade357f10e3b6031e1eb0b47bd3193b7a7cacd563473260ec446f659',
        'source_image': 'sha256:e370b9073ae2c6d362ea1854d61dab58a9be836602a2336da45037ea9eebfdc1',
        'regression': ['--deselect=tests/test_script.py::test_whitelists_with_python']},
}

CASES = {
    'toml_missing': case('development', 'file API compatible feature', [
        '只读审查 TOMLFile.read/write：读取全 CRLF 文件后修改再写回，和从未 read 就 write，有哪些不同？路径不存在、不是合法 TOML 或不能解码时现在如何处理？结合源码和必要的运行证据回答，先别改文件。',
        '我们希望配置文件还没创建时也方便使用，但不能把真正的损坏悄悄当成空配置。先列出需要确定的行为并提出兼容设计，暂不实现。',
        '需求确定：TOMLFile.read 增加 keyword-only missing_ok=False。默认仍抛缺失异常；仅 missing_ok=True 且文件不存在时返回新的空 TOMLDocument，不创建文件。权限、目录、UTF-8 解码和 TOML 解析错误仍传播；已有换行符保留行为不变。现在实现、补回归测试和简短文档，并实际验证。',
    ], [], [
        'Existing read remembers LF/CRLF/mixed; a fresh instance uses os.linesep. Missing, decoding and parsing errors propagate; avoid blanket byte-for-byte guarantees after arbitrary mutation.',
        'Distinguish missing file from damaged/unreadable file and a new optional behavior from a pre-existing defect. Propose default-compatible policy without editing.',
        'Keyword-only opt-in, fresh empty document only on missing file, no file creation, other errors propagate, EOL retained; tests and docs with truthful verification.',
    ], r'''from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from tomlkit.toml_file import TOMLFile
from tomlkit.toml_document import TOMLDocument
from tomlkit.exceptions import TOMLKitError
with TemporaryDirectory() as d:
    p=Path(d)/'settings.toml'; f=TOMLFile(p)
    try: f.read()
    except FileNotFoundError: pass
    else: raise AssertionError('default missing must raise')
    one=f.read(missing_ok=True); two=f.read(missing_ok=True)
    assert isinstance(one,TOMLDocument) and not one and one is not two and not p.exists()
    one['x']=1
    assert not two
    try: f.read(True)
    except TypeError: pass
    else: raise AssertionError('flag must be keyword-only')
    for raw,exc in [(b'k = [',TOMLKitError),(b'k = "\xff"',UnicodeError)]:
        p.write_bytes(raw)
        try: f.read(missing_ok=True)
        except exc: pass
        else: raise AssertionError('damaged input must propagate')
    with patch('builtins.open',side_effect=PermissionError('denied')):
        try: f.read(missing_ok=True)
        except PermissionError: pass
        else: raise AssertionError('permission errors must propagate')
    try: TOMLFile(Path(d)).read(missing_ok=True)
    except IsADirectoryError: pass
    else: raise AssertionError('directory errors must propagate')
    for eol in [b'\n',b'\r\n']:
        p.write_bytes(b'# keep'+eol+b'x = 1 # inline'+eol)
        item=TOMLFile(p); doc=item.read(missing_ok=True); doc['x']=2; item.write(doc)
        assert p.read_bytes()==b'# keep'+eol+b'x = 2 # inline'+eol
print('feature acceptance passed')
'''),
    'toml_sort_docs': case('development', 'correct implementation and docs-only task', [
        '只读比较 dumps(mapping, sort_keys=True) 和 dumps(parse(text), sort_keys=True)。同事认为后者也会重新排序已有 TOMLDocument，并且注释一定丢失；请检查该判断，用包含注释和逆序 key 的例子验证，先不要改文件。',
        '假设我们要在保留用户排版与生成规范排序的新配置之间选择，应怎样区分两条路径？顶层普通 dict 内嵌已有 Table/InlineTable 时，能否无条件宣称递归排序所有 key？只讨论现有行为和限制。',
        '实现保持原样，只新增 docs/serialization-policy.md，解释上述两条路径及 sort_keys 的边界，给可运行的短例子和实际输出。不要更改产品代码或测试，也不要把需求建议写成现有功能。',
    ], [], [
        'The Mapping conversion branch sorts plain mappings; existing Container/Table/InlineTable bypass conversion, so parsed document order and comments remain. Provides executed evidence, no false bug claim.',
        'Separates style-preserving round-trip from rebuilding data. Existing embedded TOML items can retain their internal ordering; no unconditional recursive sorting guarantee or universal comment-loss claim.',
        'Only requested doc added, accurate scope and executable examples whose output agrees with observations; no code changes or fabricated tests.',
    ], None),
    'vulture_noqa': case('transfer', 'annotation tokenization repair', [
        '用户报告 import os  # noqa: V104 V107 和 import sys  # NoQA: v104 仍被报告为未使用。请沿 parse_noqa 到 Vulture.scan 的路径定位原因，区分匹配到注释与最终抑制生效，先不修改。',
        '目标规则是：代码列表允许逗号或空白分隔，代码不区分大小写，F401/F841 别名仍有效；未知代码不能变成“忽略全部”。现有无代码 # noqa 行为保持。先说明兼容修复和验证范围，不实现。',
        '现在按刚才规则实现，补单元测试与一次 Vulture.scan 集成验证，并补文档。请控制修改范围，保留未提及的注释识别方式。',
    ], [], [
        'Regex recognizes whitespace-separated/lowercase codes, but comma-only split and case-sensitive matching/alias mapping stop suppression; traces actual scan behavior.',
        'Normalize and tokenize recognized codes, retain aliases and bare-noqa behavior, unknown code stays scoped rather than becoming all; do not redesign annotation recognition.',
        'Mixed separators/case plus both aliases work end-to-end, unknown codes do not suppress unrelated issues, old behavior/regressions retained with observed validation.',
    ], r'''from vulture.noqa import parse_noqa,ignore_line
from vulture.core import Vulture
for line in ['# noqa: V104 V107','# noqa: v104, v107','# NoQA: F401\tF841','# noqa: V104,\t v107']:
    parsed=parse_noqa([line])
    assert ignore_line(parsed,1,'V104') and ignore_line(parsed,1,'V107'),line
for suffix in ['# noqa','# noqa: V104 V107','# NoQA: v104','# noqa: f401','# noqa: UNKNOWN999, F401']:
    v=Vulture();v.scan('import os  '+suffix+'\n',filename='production.py')
    assert not v.unused_imports,(suffix,v.unused_imports)
for suffix in ['# noqa: V107','# noqa: UNKNOWN999','#noqa']:
    v=Vulture();v.scan('import os  '+suffix+'\n',filename='production.py')
    assert [x.name for x in v.unused_imports]==['os'],suffix
v=Vulture();v.scan('def used():\n    local = 3  # noqa: f841\nused()\n',filename='production.py')
assert not v.unused_vars
print('annotation acceptance passed')
'''),
    'vulture_min_size': case('transfer', 'cross-file API CLI configuration feature', [
        '只读说明 get_unused_code 与 report 现在如何组合 confidence 过滤、size 排序、输出和退出码。sort_by_size 是不是会自动过滤短项目？请以源码或实际例子支持判断，不改文件。',
        '想减少短条目的噪音，新增独立 min_size 下限。先设计 get_unused_code、report、CLI 和 pyproject 配置之间如何一致工作，保持现有调用、排序、confidence 和 whitelist 行为，暂不实现。',
        '现在实现：API get_unused_code 和 report 增加 min_size=0；CLI --min-size 与 [tool.vulture].min_size 支持非负整数，CLI 显式值覆盖配置。只报告 size >= min_size 的 Item，与 confidence 过滤取交集，不改变原排序。默认完全兼容；API 对负数/非整数（含 bool）抛 ValueError，CLI/配置错误按现有输入错误方式退出，不能 traceback。补文档和回归测试并验证 CLI。',
    ], [], [
        'Confidence filter is inclusive, size sorting is ascending and does not filter; report returns existing exit status and flags DeadCode only for emitted items. Avoid general claims that report resets earlier status.',
        'Filter dimension independent of ordering and confidence, API/report/CLI/config propagation, explicit zero override, stable defaults and whitelist representation retained.',
        'End-to-end min_size filtering, intersections/order, default compatibility and strict validation all hold; truthful testing and no out-of-scope changes.',
    ], r'''import contextlib,io,subprocess,sys,tempfile
from pathlib import Path
from vulture.core import Vulture
from vulture.config import make_config,InputError
code='import os\ndef tiny():\n    pass\ndef large():\n    a=1\n    b=2\n    return a+b\n'
v=Vulture();v.scan(code,filename='production.py')
for confidence in [0,60,90,100]:
    for order in [False,True]:
        baseline=v.get_unused_code(min_confidence=confidence,sort_by_size=order)
        for size in [0,1,2,3,4,9]:
            got=v.get_unused_code(min_confidence=confidence,sort_by_size=order,min_size=size)
            assert got==[x for x in baseline if x.size>=size],(confidence,order,size)
for bad in [-1,1.5,True,None,'2']:
    for fn in [v.get_unused_code,v.report]:
        try: fn(min_size=bad)
        except ValueError: pass
        else: raise AssertionError(('invalid min_size',bad))
fresh=Vulture();fresh.scan(code,filename='production.py')
with contextlib.redirect_stdout(io.StringIO()) as out:
    status=fresh.report(min_size=999)
assert not out.getvalue() and status==0
with contextlib.redirect_stdout(io.StringIO()) as out:
    status=fresh.report(min_size=3,make_whitelist=True)
assert 'large' in out.getvalue() and 'tiny' not in out.getvalue() and status==3
for bad in ['-1','true','"2"','1.5']:
    config=io.BytesIO(('[tool.vulture]\npaths=["x.py"]\nmin_size='+bad).encode())
    try: make_config([],tomlfile=config)
    except InputError: pass
    else: raise AssertionError(('invalid config',bad))
with tempfile.TemporaryDirectory() as d:
    p=Path(d)/'code.py';p.write_text(code)
    cfg=Path(d)/'p.toml';cfg.write_text('[tool.vulture]\nmin_size=99\n')
    base=[sys.executable,'-m','vulture','--config',str(cfg),str(p)]
    proc=subprocess.run(base,capture_output=True,text=True)
    assert proc.returncode==0 and not proc.stdout,proc.stdout+proc.stderr
    proc=subprocess.run(base+['--min-size','3'],capture_output=True,text=True)
    assert proc.returncode==3 and 'large' in proc.stdout and 'tiny' not in proc.stdout,proc.stdout+proc.stderr
    proc=subprocess.run(base+['--min-size','0'],capture_output=True,text=True)
    assert proc.returncode==3 and 'tiny' in proc.stdout and 'os' in proc.stdout
    proc=subprocess.run(base+['--min-size','-1'],capture_output=True,text=True)
    assert proc.returncode!=0 and 'Traceback' not in proc.stderr+proc.stdout
print('filter acceptance passed')
'''),
}

_BASE_ACCEPTANCE = {key: value['acceptance'] for key, value in CASES.items()}
CASE_REPO = {key: ('tomlkit' if key.startswith('toml_') else 'vulture') for key in CASES}


def load_fixtures(root: Path) -> tuple[dict, dict]:
    """Only decode the pinned Git archives, not arbitrary edited checkout files."""
    import tarfile
    import io
    fixtures, identity = {}, {}
    for repo, spec in REPOS.items():
        raw = (root / (repo + '.tar')).read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != spec['archive_sha256']:
            raise ValueError(f'{repo}: archive differs from frozen source')
        files = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for entry in archive:
                if entry.isdir():
                    continue
                name = entry.name
                if not entry.isfile() or Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('unsafe/non-file repository archive member')
                files[name] = archive.extractfile(entry).read().decode('utf-8')
        identity[repo] = {**spec, 'files': len(files),
            'files_sha256': hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()}
        for key, value in CASES.items():
            if CASE_REPO[key] != repo:
                continue
            fixtures[key] = dict(files)
            value['environment'] = ('Environment: Linux; bash; /workspace is the complete tracked repository '
                '(no Git history; TOML test submodule is omitted). Python 3.12, pytest and PyYAML are available offline. '
                'Use python3 -m pytest -o addopts= -p no:cacheprovider; '
                'tomlkit tests/test_toml_tests.py needs the omitted submodule and can be excluded. '
                'Vulture tests/test_script.py::test_whitelists_with_python needs optional third-party packages and can be deselected. '
                'No network or dependency installation is needed.')
            value['acceptance_timeout'] = 120
            # Original regressions are rehydrated in a fresh verifier so editing
            # tests cannot make the hidden acceptance easier.
            original_tests = {n:s for n,s in files.items() if n.startswith('tests/')}
            if key == 'vulture_min_size':
                # This old test asserts the entire config dict; the requested
                # schema addition needs one new expected default, frozen here.
                name = 'tests/test_config.py'
                old = '        min_confidence=20,\n'
                if original_tests[name].count(old) != 1:
                    raise ValueError('unexpected upstream config expectation')
                original_tests[name] = original_tests[name].replace(old, old + '        min_size=0,\n')
            regression = REPOS[repo]['regression']
            prefix = ('from pathlib import Path\nimport subprocess,sys\n'
                'for name,body in '+repr(original_tests)+'.items():\n'
                '    p=Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)\n'
                'p=subprocess.run([sys.executable,"-m","pytest","-q","-o","addopts=","-p","no:cacheprovider",'
                + ','.join(repr(x) for x in regression) + '],capture_output=True,text=True)\n'
                'print(p.stdout);print(p.stderr);assert p.returncode==0,"upstream regression failed"\n')
            own = _BASE_ACCEPTANCE[key] or ''
            if key == 'toml_sort_docs':
                own = ('from pathlib import Path\n'
                    'current={str(p):p.read_bytes().decode("utf-8") for p in Path(".").rglob("*") if p.is_file() '
                    'and not any(x in p.parts for x in ("__pycache__",".pytest_cache"))}\n'
                    'initial='+repr(files)+'\n'
                    'assert {k:v for k,v in current.items() if k!="docs/serialization-policy.md"}==initial\n'
                    'assert current.get("docs/serialization-policy.md","").strip()\n')
                # Scope must be checked BEFORE original test restoration.
                value['acceptance'] = own + prefix
            else:
                value['acceptance'] = own + prefix
    return fixtures, identity
