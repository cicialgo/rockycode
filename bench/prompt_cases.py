"""Small, bilingual multi-turn development set; never a held-out benchmark."""

FIXTURES = {
    "explain": {
        "app.py": '''def average(values):
    return sum(values) / len(values)


def label(values):
    try:
        return f"Average: {average(values):.1f}"
    except ValueError:
        return "No values"
''',
    },
    "review": {
        "app.py": '''def discounted_total(total, percent):
    """Apply a percentage discount; percent is between 0 and 100."""
    return total - percent
''',
        "test_app.py": '''import unittest
from app import discounted_total


class TestDiscount(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(discounted_total(80, 0), 80)
''',
    },
    "plan": {
        "app.py": '''def normalize_labels(labels):
    """Trim and lowercase labels, dropping empty ones; keep duplicates."""
    return [s.strip().lower() for s in labels if s.strip()]
''',
        "test_app.py": '''import unittest
from app import normalize_labels


class TestLabels(unittest.TestCase):
    def test_existing_behavior(self):
        self.assertEqual(normalize_labels([" A ", "a", " "]), ["a", "a"])
''',
    },
    "clarify": {
        "app.py": '''def checkout_total(prices):
    """Return the sum of item prices, with no discount."""
    return sum(prices)
''',
        "test_app.py": '''import unittest
from app import checkout_total


class TestCheckout(unittest.TestCase):
    def test_small_order(self):
        self.assertEqual(checkout_total([20, 30]), 50)
''',
    },
}

# Each conversation is replayed in one Engine; only the last turn in the
# editing cases authorizes code changes. Full write tools remain available.
CASES = {
    "explain": {
        "en": [
            "How does average in app.py behave for an empty list? Explain with an example.",
            "Compare returning None with raising ValueError for empty input. What changes for callers?",
            "What would the CURRENT label function do for that empty input? Explain only.",
        ],
        "zh": [
            "app.py 里的 average 遇到空列表会怎样？用例子解释一下。",
            "比较一下空输入返回 None 和抛出 ValueError 两种选择，对调用者分别有什么影响？",
            "当前的 label 函数遇到这个空输入会怎样？只解释。",
        ],
        "write_turns": [],
        "acceptance": None,
    },
    "review": {
        "en": [
            "Review discounted_total in app.py for correctness. Tell me any concrete finding.",
            "Why does a total of 100 hide that problem? Show one input that exposes it.",
            "Now fix that percentage calculation, keep the function signature, and run the tests.",
        ],
        "zh": [
            "审查 app.py 中 discounted_total 的正确性，把具体发现告诉我。",
            "为什么总价为 100 时看不出这个问题？举一个能暴露问题的输入。",
            "现在修复这个百分比计算，保持函数签名不变，并运行测试。",
        ],
        "write_turns": [3],
        "acceptance": '''from math import isclose
from app import discounted_total as f
for total, percent, expected in [(200,10,180),(80,25,60),(50,100,0),(19.5,20,15.6),(80,0,80)]:
    assert isclose(f(total,percent),expected,rel_tol=1e-9,abs_tol=1e-9), (total,percent,f(total,percent))
print("acceptance passed")
''',
    },
    "plan": {
        "en": [
            "Plan an optional unique flag for normalize_labels in app.py. Do not implement yet.",
            "Default behavior must keep duplicates; unique=True must preserve first occurrence order after normalization. Update the plan only, no file changes.",
            "Implement that plan now with unique=False as the default, preserve the existing callers, and test it.",
        ],
        "zh": [
            "为 app.py 的 normalize_labels 设计一个可选的 unique 参数，先给计划，不要实现。",
            "默认行为必须保留重复项；unique=True 则要在标准化后按首次出现顺序去重。只更新计划，不改文件。",
            "现在实现这个计划，unique 默认 False，保持现有调用兼容，并验证。",
        ],
        "write_turns": [3],
        "acceptance": '''from app import normalize_labels as f
x=[" B ","a","b"," A "," "]
assert f(x)==["b","a","b","a"]
assert f(x,unique=False)==["b","a","b","a"]
assert f(x,unique=True)==["b","a"]
assert f([],unique=True)==[]
assert x==[" B ","a","b"," A "," "]
print("acceptance passed")
''',
    },
    "clarify": {
        "en": [
            "Add a discount policy to checkout_total. I haven't decided the percentage or eligibility threshold. Help me settle those before implementation.",
            "Use 10 percent off when the subtotal is at least 100, then round the final total to two decimal places. First explain the boundary cases; don't implement yet.",
            "Yes, implement that agreed policy now, keep the existing signature, and run tests.",
        ],
        "zh": [
            "给 checkout_total 加优惠规则，折扣比例和门槛我还没定。实现前先帮我把这两个决定弄清楚。",
            "小计至少 100 才打九折，最终总价四舍五入保留两位小数。先解释边界情况，暂时别实现。",
            "对，现在实现约定好的规则，保持现有函数签名，并运行测试。",
        ],
        "write_turns": [3],
        "acceptance": '''from math import isclose
from app import checkout_total as f
for prices, expected in [([],0),([20,30],50),([99.99],99.99),([100],90),([60,60],108),([100.03],90.03)]:
    assert isclose(f(prices),expected,rel_tol=1e-9,abs_tol=1e-9), (prices,f(prices))
print("acceptance passed")
''',
    },
}
