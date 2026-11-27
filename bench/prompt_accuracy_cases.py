"""Frozen cross-task accuracy pilot; rubrics/oracles never enter model context.

Development and transfer families are declared before sampling. Translations
and repetitions are correlated observations, not independent benchmark items.
"""

ACCURACY_RULES = {
    'evidence': {
        'en': '- Ground factual claims in the code, documented contract, or observed output. Distinguish assumptions and proposed behavior from the current behavior; claim a check passed only when you observed it.',
        'zh': '- 用代码、文档约定或实际输出支持事实判断。区分假设、建议行为和当前行为；只有实际观察到检查结果，才说检查通过。',
    },
    'counterexample': {
        'en': '- Before a broad correctness claim, check a relevant boundary or counterexample. A documented precondition is not automatically a missing-validation bug; do not invent a defect when the behavior meets the requested contract.',
        'zh': '- 做普遍性的正确性判断前，检查一个相关边界或反例。文档前置条件不自动等于缺少校验的 bug；行为符合需求约定时，不要制造问题。',
    },
}
ACCURACY_ARMS = {'adapted': (), 'evidence_only': ('evidence',),
                 'counterexample_only': ('counterexample',), 'grounded': ('evidence', 'counterexample')}
FIXTURES = {
    'interval': {'app.py': 'def overlaps(a, b):\n    """Closed intervals: endpoints are included; each start <= its end."""\n    return a[0] < b[1] and b[0] < a[1]\n'},
    'lookup_contract': {'app.py': 'def lookup(mapping, key):\n    """Return a value. Callers guarantee that key exists."""\n    return mapping[key]\n',
                        'README.md': 'lookup requires an existing key. Missing-key behavior is outside its contract.\n'},
    'config_layers': {'app.py': 'def resolve_timeout(cli, env, config):\n    """None means absent; zero disables the timeout. Precedence: CLI, env, config."""\n    return cli or env or config\n'},
    'lazy_parse': {'app.py': 'def parse(values):\n    try:\n        return (int(value) for value in values)\n    except ValueError:\n        return []\n',
                   'caller.py': 'from app import parse\n\ndef render(values):\n    return list(parse(values))\n'},
    'sql_counts': {'schema.sql': 'CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);\nCREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT);\n',
                   'query.sql': 'SELECT c.id, COUNT(o.id) AS order_count\nFROM customers c LEFT JOIN orders o ON o.customer_id = c.id\nGROUP BY c.id ORDER BY c.id;\n'},
    'stable_ranking': {'app.py': 'def rank(rows):\n    """Descending score; preserve input order for ties."""\n    return sorted(rows, key=lambda row: row["score"], reverse=True)\n\ndef alternative(rows):\n    return list(reversed(sorted(rows, key=lambda row: row["score"])))\n'},
    'retry_docs': {'app.py': 'def attempt(action, retries):\n    """retries is a nonnegative count of additional attempts."""\n    for index in range(retries + 1):\n        try:\n            return action()\n        except ValueError:\n            if index == retries:\n                raise\n',
                   'README.md': 'attempt(action, retries) retries ValueError.\n'},
    'copy_state': {'app.py': 'def draft(record):\n    """Return a shallow copy of the record."""\n    return dict(record)\n'},
}


def case(split, family, zh, en, rubric, acceptance, write_turns=(3,)):
    return dict(split=split, family=family, zh=zh, en=en, rubric=rubric,
                acceptance=acceptance, write_turns=list(write_turns))


CASES = {
    'interval': case('development', 'numeric boundary review', [
        '审查 app.py 的 overlaps 是否符合文档约定，给出具体证据，先不要修改。',
        '现在两个区间有内部重叠时测试都通过了，这能证明实现正确吗？只分析。',
        '现在修复已确认的约定偏差，保留签名，并验证端点和零长度区间。',
    ], [
        'Review whether overlaps in app.py meets its documented contract. Give evidence, without editing yet.',
        'Tests with interior overlap all pass. Does that establish correctness? Analyze only.',
        'Now fix the confirmed contract mismatch, preserve the signature, and verify endpoints and zero-length intervals.',
    ], [
        'Finds strict inequalities exclude touching closed endpoints; offers a concrete example.',
        'Passing interior examples is insufficient; identical point intervals also expose the mismatch.',
        'Uses inclusive comparisons and actually verifies; does not change the precondition into a new validation requirement.',
    ], '''from app import overlaps
from itertools import combinations_with_replacement
intervals=list(combinations_with_replacement(range(-3,4),2))
for a in intervals:
    for b in intervals:
        assert overlaps(a,b) == (max(a[0],b[0]) <= min(a[1],b[1])), (a,b)
print('acceptance passed')
'''),
    'lookup_contract': case('development', 'correct code and changed contract', [
        '审查 lookup 的正确性，结合 README 中的调用约定。先说明发现，不改文件。',
        '现在想支持缺失 key：未传 default 时仍抛 KeyError；显式 default=None 时返回 None。先说明兼容设计，不实现。',
        '按刚才约定实现可选 default 参数，保留已有调用和已有值为 None 的行为，运行验证。',
    ], [
        'Review lookup against the README caller contract. Explain findings without edits.',
        'We now want missing keys supported: omitting default must still raise KeyError; explicit default=None returns None. Explain a compatible design only.',
        'Implement the agreed optional default argument, preserve existing calls and stored None values, and verify.',
    ], [
        'Existing implementation meets guaranteed-key contract; missing-key validation is not a confirmed bug.',
        'Separates changed requirements from old defects; distinguishes absent default from explicit None with a sentinel or equivalent.',
        'Existing keys win even for None; omitted default raises, explicit default returned only for missing keys; real verification.',
    ], '''from app import lookup
m={'x':None,'y':0}
assert lookup(m,'x') is None and lookup(m,'y',99)==0
assert lookup(m,'missing',None) is None
s=object()
assert lookup(m,'missing',s) is s
try: lookup(m,'missing')
except KeyError: pass
else: raise AssertionError('omitted default must raise')
assert m=={'x':None,'y':0}
print('acceptance passed')
'''),
    'config_layers': case('development', 'configuration precedence', [
        '只解释 resolve_timeout 当前如何选择值，并判断是否符合注释约定。',
        '同事说只要 cli 不是 None 就一定用 cli；我们用 cli=5 的测试通过了。这个说法成立吗？先不要改。',
        '现在按文档修复优先级，保留 0 禁用超时的语义和函数签名，验证各层覆盖关系。',
    ], [
        'Explain how resolve_timeout currently chooses a value and whether it follows its docstring. Do not edit.',
        'A colleague says any cli other than None always wins; our cli=5 test passes. Is that claim valid? No edits yet.',
        'Now fix precedence according to the contract, preserve zero as disabling timeouts and the signature, and verify layer overrides.',
    ], [
        'or selects truthy values; zero may be skipped, contrary to None-only absence.',
        'cli=0 with a nonzero fallback disproves the claim; does not assert zero always changes the result when lower layers equal zero.',
        'First non-None value, including zero at any layer; actual tests and truthful reporting.',
    ], '''from app import resolve_timeout
from itertools import product
for values in product([None,0,1,9], repeat=3):
    expected=next((x for x in values if x is not None),None)
    assert resolve_timeout(*values)==expected, values
print('acceptance passed')
'''),
    'lazy_parse': case('development', 'cross-file exception timing', [
        '只读分析 caller.render(["4", "bad"]) 会返回什么或抛什么，说明异常在哪一步发生。',
        'parse 内明明有 except ValueError，为什么可能没有兜住？和空列表、只调用 parse 不遍历这两种情况比较。暂不改。',
        '需求确定了：parse 要立即把输入转换成整数列表；有任一非法项就返回空列表，不能返回部分结果。现在实现并验证调用者。',
    ], [
        'Read-only: what does caller.render(["4", "bad"]) return or raise, and when?',
        'Why might except ValueError in parse not catch it? Compare empty input and calling parse without consuming it. No edits.',
        'Settled requirement: parse must eagerly return an integer list, or an empty list if any item is invalid, with no partial results. Implement and verify the caller.',
    ], [
        'ValueError occurs during list consumption in render, after parse has returned its generator.',
        'Generator creation does not convert values; empty consumption succeeds; standalone parse defers the error.',
        'Eager conversion catches invalid strings without returning a partial list; behavior of caller verified.',
    ], '''from app import parse
from caller import render
for xs, expected in [([],[]),(['4','5'],[4,5]),(['4','bad'],[]),(['bad','4'],[]),(['-2','0'],[-2,0])]:
    actual=parse(xs)
    assert isinstance(actual,list) and actual==expected
    assert render(xs)==expected
print('acceptance passed')
'''),
    'sql_counts': case('transfer', 'SQL outer join and NULL', [
        '阅读 SQL 文件，解释没有订单的客户是否会出现在结果里，以及 order_count 是多少。先不要修改。',
        '如果把 COUNT(o.id) 改成 COUNT(*)，或者把 o.status="paid" 放进 WHERE，各自会改变什么？只比较。',
        '现在把 query.sql 改为统计已付款订单，同时保留所有客户，包括零订单和只有未付款订单的客户。验证结果。',
    ], [
        'Read the SQL files and explain whether customers with no orders appear, and their order_count. No edits.',
        'Compare replacing COUNT(o.id) with COUNT(*), and putting o.status="paid" in WHERE. Explain only.',
        'Update query.sql to count paid orders while retaining every customer, including those with no orders or only unpaid ones. Verify.',
    ], [
        'LEFT JOIN retains unmatched customers; COUNT(non-null id) is zero, not one.',
        'COUNT(*) counts the null-extended row; WHERE paid removes unmatched customers and those without paid matches.',
        'Filters in ON or equivalent conditional aggregation, retains zeros; evidence from query execution.',
    ], '''import sqlite3
from pathlib import Path
c=sqlite3.connect(':memory:')
c.executescript(Path('schema.sql').read_text())
c.executemany('INSERT INTO customers VALUES (?,?)',[(i,str(i)) for i in range(1,6)])
c.executemany('INSERT INTO orders VALUES (?,?,?)',[(1,2,'unpaid'),(2,3,'paid'),(3,3,'paid'),(4,3,'unpaid'),(5,4,'paid'),(6,5,None)])
assert sorted(c.execute(Path('query.sql').read_text()).fetchall())==[(1,0),(2,0),(3,2),(4,1),(5,0)]
print('acceptance passed')
'''),
    'stable_ranking': case('transfer', 'sorting and feature compatibility', [
        '比较 rank 和 alternative 是否总是给出相同顺序，并判断 rank 是否遵守文档。只分析不修改。',
        '两个不同对象分数相同时会怎样？另外，没有并列分数时，两个实现还会不同吗？用例子说明。',
        '现在给 rank 增加可选 limit，默认 None 返回全部，非负整数限制条数。保留降序和并列时输入顺序，不改 alternative，验证。',
    ], [
        'Compare whether rank and alternative always give the same order, and whether rank meets its docstring. Analyze without editing.',
        'What happens for distinct objects with equal scores? Do the implementations still differ when all scores are unique? Explain with examples.',
        'Add optional limit to rank: None by default returns everything, a nonnegative integer caps the count. Preserve descending scores and input order for ties; leave alternative unchanged and verify.',
    ], [
        'reverse=True sort remains stable; reversing an ascending result reverses ties. rank itself is correct.',
        'Shows distinct tied objects; for unique scores both orders agree; avoids claiming all ties necessarily change observable equal objects.',
        'limit=0 yields empty, None yields full ranking; no mutation, stable ties, real verification.',
    ], '''from app import rank, alternative
from itertools import product
for scores in product([0,1,2], repeat=4):
    xs=[{'id':i,'score':s} for i,s in enumerate(scores)]
    original=list(xs)
    expected=sorted(xs,key=lambda x:-x['score'])
    for limit in [None,0,1,3,9]:
        assert rank(xs,limit=limit)==(expected if limit is None else expected[:limit])
    assert rank(xs)==expected and xs==original
    assert alternative(xs)==list(reversed(sorted(xs,key=lambda x:x['score'])))
print('acceptance passed')
'''),
    'retry_docs': case('transfer', 'correct control flow and documentation task', [
        '审查 attempt 在 retries=0 和 retries=2 时的最大调用次数，是否存在 off-by-one？只读回答。',
        '第一次就成功时一定会用完这些次数吗？如果抛 TypeError 呢？只解释，不要改实现。',
        '实现保持原样，只更新 README.md：说明 retries 的含义、最大与实际次数、ValueError 和 TypeError 的区别，并加简短示例。',
    ], [
        'Read-only review: maximum calls for retries=0 and retries=2; is there an off-by-one error?',
        'Does immediate success always consume that maximum? What about TypeError? Explain without implementation changes.',
        'Keep implementation unchanged; update only README.md with retry meaning, maximum versus actual calls, ValueError versus TypeError, and short examples.',
    ], [
        'Maximum 1 and 3 calls; retries counts additional attempts, so no off-by-one defect.',
        'Success returns early; TypeError propagates immediately without retry; maxima are conditional, not guaranteed counts.',
        'Only README changes, accurate documentation with examples; no claim of tests unless run.',
    ], None),
    'copy_state': case('transfer', 'aliasing and evolving requirements', [
        '解释 draft 返回值与原 record 的关系：外层字典和嵌套列表分别是否共享？按当前文档这算 bug 吗？先不要改。',
        '需求改为让编辑草稿的嵌套内容完全不影响原记录；记录可能含 list、dict、set。先比较可行方案，不实现。',
        '现在实现刚才的新需求，保持 draft 签名，不通过 JSON 转换丢失集合类型，验证嵌套修改互不影响。',
    ], [
        'Explain draft aliasing: outer dictionary versus nested lists. Is it a bug under the current docstring? No edits.',
        'New requirement: editing nested draft contents must not affect the original; records may contain lists, dictionaries, and sets. Compare approaches only.',
        'Implement the new requirement with the same draft signature and without losing set types via JSON conversion. Verify independent nested edits.',
    ], [
        'Outer dict new, nested references shared; this is documented shallow-copy behavior, not a confirmed original defect.',
        'Deep copy fits the new container requirement; distinguishes changed requirements; JSON cannot preserve arbitrary sets.',
        'Independent nested contents in both directions; preserves set types, verifies, updates stale shallow-copy docstring.',
    ], '''from app import draft
record={'a':[{'tags':{'x'}}],'b':{'v':[1]}}
x=draft(record)
x['a'][0]['tags'].add('y')
x['b']['v'].append(2)
assert record=={'a':[{'tags':{'x'}}],'b':{'v':[1]}}
record['a'].append({'tags':set()})
assert len(x['a'])==1 and isinstance(x['a'][0]['tags'],set)
assert draft({})=={}
print('acceptance passed')
'''),
}
# Docs-only oracle compares code bytes rather than prescribing README phrasing.
CASES['retry_docs']['acceptance'] = (
    'from pathlib import Path\n'
    f"assert Path('app.py').read_text()=={FIXTURES['retry_docs']['app.py']!r}\n"
    f"assert Path('README.md').read_text()!={FIXTURES['retry_docs']['README.md']!r}\n"
    "print('acceptance passed; prose accuracy requires review')\n"
)
