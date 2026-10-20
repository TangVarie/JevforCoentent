# -*- coding: utf-8 -*-
"""规则编译器的 forbidden_regex 和写作台收的一样宽（2026-10-20）。

写作台存规则前（autowriter deskcore/rulecheck.normalize_check）查两道：validator.assert_regex_is_safe（嵌套无上界量词、
aw#115 加的「挨在一起、能匹配同样字符的量词」）和 rulecheck._assert_regex_shape（量词只套单字符 / 定长不带分支的组）。
Jev 编出来的 code 写作台拒收的话，这条规则的代码检查就静默没了；所以 judge/rules.validate_code 得把写作台拒收的全部打回
（模型带着原因重编，还不行这一条带 error），写作台收的、PROMPT_HEAD 教的写法又一个都不能误伤。

下面的清单原样抄自 autowriter 的 tests/test_regex_safety.py 和 tests/test_rulecheck.py（aw#115 892769b，2026-10-20：含 codex review on aw#115 那几轮加的「组里的旗」「(?i) 下的字集交给 re 判」
「条件组」，和最新一轮的「前瞻 / 占有量词 /
原子组不是隔断」「可选组里的量词和组外的照样挨着」几组用例），放行 / 拒收按写作台 normalize_check 整体的结论分（两道都过才算放行）。
写作台改了闸，这里跟着换清单。另有两张对拍探针（拿写作台和 Jev 跑同一批随机正则）抓到过两边不一致的清单，钉住别再回来
（在 lookahead/possessive 这一轮上重新核过：两条原来写作台放行的现在写作台拒收，挪进 PROBE_NOW_REJECTED）。
全部是合成的正则和正文，不含任何客户原文。
"""
from __future__ import annotations

import json
import re
import time

import pytest

from judge import rules as R

# ── 写作台拒收的（normalize_check 抛 CheckError，或者压根编译不了）────────────────────────────────────────────────

# test_regex_safety.test_catastrophic_shapes_are_rejected：嵌套无上界量词
DESK_CATASTROPHIC = [
    "(a+)+$",
    "(a*)*",
    "(a+)*b",
    "(.*)*x",
    "((ab)+)+",
    "(?:\\d+)+$",
    "(a+)+" + "c",
]

# test_regex_safety.test_adjacent_overlapping_repeats_are_rejected：挨在一起、能匹配同样字符的量词（aw#115）
DESK_ADJACENT = [
    "a*a*a*a*a*a*a*a*b",
    ".*.*x",
    "\\w+\\w+x",
    "[哈呵]*哈*[呵哈]*。",
    "a*b*a*c",
    "a.*a.*a.*b",
    "(a*)a*b",
    "(?:x|a*)a*b",
    "a{0,30}a{0,30}a{0,30}b",
    "a?" * 14 + "a" * 14,
    "(?i)A*a*b",
    "(?:.{0,20}){5}x",
    "(?:.{0,20}){0,5}x",
    "(?:哈{0,9}){6}!",
    "(?:\\w{1,5})+x",
    "(?:.{0,20})*x",
    "(?:x.*x.*x)y",
    "!.*!.*!$",
    "\\w*\\w*\\d+",
    "!.{100,}!.{100,}!.{100,}!",
    "[!！]" + "[\\s\\S]*[!！]" * 15,
    "a.*?[!！].*[!！][!！]",
]

# test_regex_safety.test_scoped_flags_are_honored：组里的旗只管组里，比字集要按组里的旗（codex review on aw#115，2eef1a6）
DESK_SCOPED_FLAGS = [
    "(?i:a*)A*B",
    "a*(?i:A)*b",
    "(?a:\\W*)é*x",
    "(?a)\\W*é*x",
    "(?s:.*)\\n*x",
]

# test_regex_safety.test_case_insensitive_sets_are_compared_the_way_re_matches：哪些字算在字集里交给 re 自己判（codex review on
# aw#115，2eef1a6）。手写的折叠只往小写折，(?i)[A-Z] 不认 a；ẞ/ß、İ/i casefold 对不上；× 夹在 À-Þ 中间，只看两头会漏
DESK_CASE_SETS = [
    "(?i)[A-Z]*(?-i:a*)x",
    "(?i)[A-Z]+(?-i:[a-z]+)\\d",
    "(?i:[A-Z]+)\\s*[a-z]+\\d",
    "[a-z]*(?i:[A-Z]*)x",
    "(?i)[À-Þ]*é*x",
    "(?i)ß*ẞ*x",
    "(?i)İ*i*x",
    "\\W*[À-Þ]*x",
]

# test_regex_safety.test_conditional_groups_are_rejected：条件组 (?(1)是|否) 闸看不进它里面（codex review on aw#115，2eef1a6）
DESK_CONDITIONAL = ["()(?(1)(a+)+b)", "()(?(1)a*a*a*a*a*b)", "(a)?(?(1)x|.*.*y)"]

# test_regex_safety.test_lookaheads_possessives_and_atomic_groups_do_not_separate：前瞻 (?=…) (?!…)、占有量词、原子组 (?>…)
# 都不是隔断（codex review on aw#115，lookahead/possessive 那一轮）。(pattern, 配多少个什么字才慢)，例子原样
DESK_LOOKAHEAD_POSSESSIVE_SLOW = [
    ("\\d+(?=\\d*元)", "1"),
    (".*(?=.*x)", "a"),
    ("\\w*(?=\\w*x)", "a"),
    ("\\w*(?!\\w*x)y", "a"),
    ("\\d+(?=\\d*)x", "1"),
    ("\\w*(?!\\w*)", "a"),
    (".*a*+.*x", "b"),
    (".*(?>a*).*x", "b"),
    (".*a++.*x", "a"),
    ("\\d+(?>x?)\\d+y", "1"),
    ("\\w*(?>a?)\\w*!", "b"),
    ("\\d+\\d++x", "1"),
    ("\\d+(?<=\\d(?=\\d*x))", "1"),
    ("\\d+(?<=(?=\\d*x)\\d)", "1"),
]
DESK_LOOKAHEAD_POSSESSIVE = [p for p, _ in DESK_LOOKAHEAD_POSSESSIVE_SLOW]

# test_regex_safety.test_repeats_inside_optional_or_counted_groups_still_meet_their_neighbours：可选 / 次数可变的组里的量词
# 和组外面的照样挨着（同一轮）。(pattern, 字, 个数)，例子原样；最后三条写作台的形状那道也拒（量词套了带量词 / 分支的组）
DESK_OPTIONAL_GROUP_SLOW = [
    ("\\d+(?:[,，]?\\d+)?元", "1", 500),
    ("\\S+(?:\\s*\\S+)?！", "中", 500),
    ("\\d+(?:\\s*\\d+)?元", "1", 500),
    ("(?:\\d+\\s*)?\\d+元", "1", 1000),
    ("(?:\\w+\\s*)?\\w+！", "中", 1000),
    ("\\d+(?=\\s*(?:\\d+)?元)", "1", 500),
    ("\\d+(?=(?:\\s*\\d+)?元)", "1", 500),
    ("\\d+(?:\\d++)?x", "1", 1000),
    ("\\d+(?:(?>\\d*))?x", "1", 1000),
    ("\\d+(?:(?=\\d*x))?y", "1", 1000),
    ("\\d*(?:a?b?){1,3}\\d*x", "1", 1000),
    ("\\d*(?:a|)+\\d*x", "1", 1000),
    ("\\d*(?:a?b?)++\\d*x", "1", 1000),
]
DESK_OPTIONAL_GROUP = [p for p, _, _ in DESK_OPTIONAL_GROUP_SLOW]

# test_regex_safety 其余拒收的：定次量词套五层（闸自己不能被拖住）、编译不了
DESK_OTHER_REJECTED = [
    "(?:(?:(?:(?:(?:a{16}){16}){16}){16}){16})",
    "(unclosed",
]

# test_rulecheck.test_bad_checks_are_rejected_with_a_reason / test_unusable_checks_are_skipped_not_fatal 里的正则
DESK_RULECHECK_REJECTED = [
    "(a+)+$",
    "(",
    "(哈|哈哈)+$",
    "(ab?)+$",
    "(?:x|yz){2,9}",
    "a*a*a*a*a*a*a*a*b",
    ".*.*x",
    "(a*)*",
]

# test_regex_safety.test_separated_or_disjoint_repeats_still_pass 里 validator 放行、但 rulecheck._assert_regex_shape 拒收的
# （量词套了带量词的组）：写作台整体拒收，Jev 也得打回
DESK_VALIDATOR_ONLY = [
    "(?:\\S+\\s+){5}\\S+",
    "(?:\\d{1,3},)+\\d{3}",
    # lookahead/possessive 那一轮：test_lookaheads_possessives_and_atomic_groups_that_stay_allowed_are_fast 里的这一条、
    # test_the_check_computes_each_char_set_once 的三条（定次量词套三层）
    "^\\S+(?:\\s+\\S+){3}",
    ".*(?=(?:(?:(?:a\\d){16}){16}){10})",
    ".*(?!(?:(?:(?:a\\d){16}){16}){10})x",
    ".*(?>(?:(?:(?:a\\d){16}){16}){10})x",
]

DESK_MUST_REJECT = list(dict.fromkeys(DESK_CATASTROPHIC + DESK_ADJACENT + DESK_SCOPED_FLAGS + DESK_CASE_SETS + DESK_CONDITIONAL
                                      + DESK_LOOKAHEAD_POSSESSIVE + DESK_OPTIONAL_GROUP
                                      + DESK_OTHER_REJECTED + DESK_RULECHECK_REJECTED + DESK_VALIDATOR_ONLY))

# ── 写作台放行的（validator 和 rulecheck 两道都过）──────────────────────────────────────────────────────────────

DESK_MUST_PASS = list(dict.fromkeys([
    # test_regex_safety.test_ordinary_patterns_still_pass
    "最",
    "(?i)best|top",
    "第[一二三]名",
    "\\d{1,20}元",
    "(ab){1,10}",
    "^标题[:：]",
    "[a-z]+@[a-z]+\\.com",
    # test_regex_safety.test_separated_or_disjoint_repeats_still_pass（去掉上面 DESK_VALIDATOR_ONLY 那两条）
    "a*b*c",
    "\\d+\\.\\d+",
    "加.{0,3}微.{0,3}信",
    "\\d{1,3}\\d{1,3}",
    ".*私信.*",
    "a*+a*b",
    "^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S",
    "品牌[\\s\\S]*品牌[\\s\\S]*品牌",
    "最(好|佳|强|便宜|有效)|第一(名|品牌)",
    "[!！][\\s\\S]*[!！][\\s\\S]*[!！][\\s\\S]*[!！]",
    "最[\\s\\S]*最[\\s\\S]*最",
    "哦[\\s\\S]*哦[\\s\\S]*哦",
    "#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#",
    "\\n\\s*\\n\\s*\\n",
    "[!！]" + "[\\s\\S]*[!！]" * 10,
    "\\d+\\s*\\d+",
    "[!！]+\\s*[!！]+",
    "原价.*\\d+",
    ".*[!！]{2,}",
    "(?:哈){5}",
    "(?:哈哈)+",
    "哈{5,}",
    # test_rulecheck.test_ordinary_patterns_still_pass_the_shape_check
    r"哈{5,}", r"[!！]{3,}", r"(?:哈哈)+", r"(哈哈)+", r"\d+%", r"(最|第一|顶级)", r"(?:啊|呀)+", r".{0,5}私信",
    # test_rulecheck.test_counting_a_single_character_is_recordable
    r"[!！][\s\S]*[!！][\s\S]*[!！][\s\S]*[!！]", r"最[\s\S]*最[\s\S]*最", r"\n\s*\n\s*\n", r"\d+\s*\d+", r"原价.*\d+",
    # test_rulecheck.test_validator_and_rulecheck_agree_on_title_and_body 等处的锚点写法
    "^禁止", "标题$", "禁止$", "^Alpha", "私信",
    # test_regex_safety.test_scoped_flags_that_narrow_the_set_still_pass（codex review on aw#115，2eef1a6）：
    # 组里关掉 (?i)、组里换回 Unicode、没有 (?s) —— 字集不相交，照放行
    "(?i)(?-i:a*)(?-i:A*)B", "(?a)(?u:\\W*)é*x", ".*\\n*x",
]))

# test_regex_safety.test_lookaheads_possessives_and_atomic_groups_that_stay_allowed_are_fast（lookahead/possessive 那一轮；
# ^\S+(?:\s+\S+){3} 写作台的形状那道拒，在 DESK_VALIDATOR_ONLY；a*+a*b 上面已经有了）。(pattern, 写作台掐表用的正文)
DESK_LOOKAHEAD_POSSESSIVE_FAST = [
    ("\\d+(?=元)", ["1" * 2000]),
    ("\\d+(?=\\s*元)", ["1" * 2000, "1" + " " * 2000]),
    ("(?=.*品牌).*折扣", ["a" * 2000, "品牌" * 1000]),
    ("(?=.*A)(?=.*B).*C", ["A" * 2000, "AB" * 1000]),
    ("\\d+(?!\\d)", ["1" * 2000]),
    ("\\w+(?=\\s)", ["a" * 2000]),
    ("(?<!\\d)\\d{11}(?!\\d)", ["1" * 2000]),
    ("[^\\n]*(?=\\n)", ["a" * 2000]),
    ("\\d+(?<=\\d)x", ["1" * 2000]),
    ("\\d+(?=\\d*)", ["1" * 2000]),
    ("^\\S+(?:\\s+\\S+){3}", ["a" * 2000, "a " * 1000]),
    ("\\d++\\.\\d+", ["1" * 2000]),
    ("(?>\\w+)\\s+\\w+", ["a" * 2000]),
    ("a*+a*b", ["a" * 2000]),
    (".*\\d++", ["a" * 2000, "1" * 2000]),
]
# test_regex_safety.test_optional_groups_that_stay_allowed_are_fast（同一轮）
DESK_OPTIONAL_GROUP_FAST = [
    ("\\d+(?:\\.\\d+)?(?=\\s*元)", ["1" * 1000, "1." * 1000, "1" + " " * 2000, "1.1" * 700]),
    ("\\d+(?:\\.\\d+)?(?=\\s*[元块])", ["1" * 1000, "1." * 1000, "1" + " " * 2000]),
    ("\\d+(?:\\.\\d+)?\\s*[元块]", ["1" * 1000, "1." * 1000, "1" + " " * 2000, "1.1" * 700]),
    ("\\d+(?:\\.\\d+)?元", ["1" * 1000, "1." * 1000]),
    ("[\\u4e00-\\u9fff]+(?:[a-z]+)?！", ["中" * 1000, "中a" * 500]),
]
DESK_MUST_PASS = list(dict.fromkeys(DESK_MUST_PASS + [p for p, _ in DESK_LOOKAHEAD_POSSESSIVE_FAST + DESK_OPTIONAL_GROUP_FAST
                                                      if p not in DESK_VALIDATOR_ONLY]))

# 对拍探针（写作台 normalize_check 对 Jev validate_code，跑上万条随机正则）2026-10-20 抓到的、两边结论不一样的：
# 都带旗（组里的 (?i:…) (?s:…) (?a:…)，或者开头的 (?a)），是 Jev 补 _scoped / ASCII 字集之前漏掉的。写作台全部拒收 / 全部放行
# （2eef1a6 上重新核过：字集改成交给 re 判以后，这几条写作台的结论一条没变）。
PROBE_DESK_REJECTS = [
    "(?a)é哈*\\W{2,}(?=！(?a:\\d{1,}\\Sé|[a-z]?#) ??(?=\\d{2,})a)?$",
    "(?a)\\W{0,20}é{2,}[a-z]\\.é++",
    "(?s)^ {2,}A(?i:\\nA{0,20}[a-z]*é(?i:哈{0,3}[\\s\\S][\\s\\S]{1,5}A{1,5}[^!]))",
    "\\S\\d*?(?s:.?\\n{2,}\\n++|.?[!！]\\d)\\D*+",
    "\\.*+ (?i:A{0,20}a\\D*)[\\s\\S]{0,20}",
    "(?s)哈*?(?a:[A-Z](?-i:\\d*é{1,}\\S[^!]\\W*?))哈++",
    "(?ia)\\s\\W*?é*?$",
    "a(?a:[A-Z]{0,3}哈*\\W+?#\\d)[a-z]{1,}",
    "(?a)哈(?>哈é[^!]+?)\\W*[A-Z]?品牌*$",
    "(?s:[!！](\\.??)??|\\W)\\n+a*(\\S(?i:品牌{3}!*?é|[^!]A*)..)$",
    "(?s)[A-Z](?a:\\d\\W{1,})é\\W{1,}é+|\\.[A-Z]",
    "(?a)#| \\W+?哈{0,20}[a-z]{2,}[\\s\\S]",
    "\\w{1,5}(?a:\\W+\\s?)\\D{0,3}$",
    "(?ia)^品牌*?\\.{0,3}\\W{0,3}[^!]{1,5} *?",
]
PROBE_DESK_ACCEPTS = [
    "(?a)(?a:[\\s\\S]??！\\S{0,3}é+?\\w?)\\sx",
    "(?a)\\D{0,3}|(?>\\n*+哈{2,}\\w+? )#",
]
# 上面那张原来还有这两条（2eef1a6 上写作台放行）。lookahead/possessive 那一轮占有量词不再是隔断，写作台改成拒收：
# 开着的 " *" 后面接 .++、开着的 [A-Z]* 后面接 A++，前面每换一种切法它就从头吃一遍。Jev 跟着拒收。
PROBE_NOW_REJECTED = [
    "(?ia) *\\D.++\\d|品牌\\w*é+A+ ",
    "(?a)品牌{1,}\\w{1,}.[a-z]|!??[A-Z]*A++\\W{1,5}A{3}",
]


def _code(p: str) -> dict:
    return {"kind": "forbidden_regex", "pattern": p, "where": ["body"]}


def test_the_two_lists_do_not_overlap():
    assert not set(DESK_MUST_REJECT) & set(DESK_MUST_PASS)
    assert (len(DESK_MUST_REJECT), len(DESK_MUST_PASS)) == (84, 61), "清单是抄来的，条数变了说明抄漏或抄多了"
    assert (len(DESK_LOOKAHEAD_POSSESSIVE), len(DESK_OPTIONAL_GROUP)) == (14, 13)
    assert (len(DESK_LOOKAHEAD_POSSESSIVE_FAST), len(DESK_OPTIONAL_GROUP_FAST)) == (15, 5)
    assert not set(PROBE_DESK_REJECTS + PROBE_NOW_REJECTED) & set(PROBE_DESK_ACCEPTS + DESK_MUST_PASS)
    assert not set(PROBE_DESK_ACCEPTS) & set(DESK_MUST_REJECT)


@pytest.mark.parametrize("pattern", DESK_MUST_REJECT)
def test_whatever_the_desk_refuses_is_sent_back_for_a_retry(pattern):
    with pytest.raises(R.RuleOutputError):
        R.validate_code(_code(pattern))


@pytest.mark.parametrize("pattern", DESK_MUST_PASS)
def test_whatever_the_desk_records_still_compiles(pattern):
    assert R.validate_code(_code(pattern))["pattern"] == pattern


@pytest.mark.parametrize("pattern", PROBE_DESK_REJECTS + PROBE_NOW_REJECTED)
def test_what_the_probe_caught_jev_letting_through_is_sent_back(pattern):
    with pytest.raises(R.RuleOutputError):
        R.validate_code(_code(pattern))


@pytest.mark.parametrize("pattern", PROBE_DESK_ACCEPTS)
def test_what_the_probe_caught_jev_refusing_compiles_again(pattern):
    assert R.validate_code(_code(pattern))["pattern"] == pattern


@pytest.mark.parametrize("pattern, ch", [                  # 写作台 test_scoped_flags_are_honored 原样：例子本身得是慢的
    ("(?i:a*)A*B", "A"),
    ("a*(?i:A)*b", "a"),
    ("(?a:\\W*)é*x", "é"),
    ("(?a)\\W*é*x", "é"),
    ("(?s:.*)\\n*x", "\n"),
])
def test_scoped_flags_are_honored_with_a_reason_the_model_can_act_on(pattern, ch):
    """组里的旗 (?i:…) (?s:…) (?a:…) 和开头的 (?a) 按它们自己管的那一段比字集；打回的话和别的「挨在一起」一样。"""
    t0 = time.perf_counter()
    re.search(pattern, ch * 1000)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的，否则测不出东西"
    with pytest.raises(R.RuleOutputError) as ei:
        R.validate_code(_code(pattern))
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern, ch", [                  # 写作台 test_case_insensitive_sets_are_compared_the_way_re_matches 原样
    ("(?i)[A-Z]*(?-i:a*)x", "a"),
    ("(?i)[A-Z]+(?-i:[a-z]+)\\d", "a"),
    ("(?i:[A-Z]+)\\s*[a-z]+\\d", "a"),
    ("[a-z]*(?i:[A-Z]*)x", "a"),
    ("(?i)[À-Þ]*é*x", "é"),
    ("(?i)ß*ẞ*x", "ß"),
    ("(?i)İ*i*x", "i"),
    ("\\W*[À-Þ]*x", "×"),
])
def test_case_insensitive_sets_are_compared_the_way_re_matches(pattern, ch):
    """哪些字算在字集里交给 re 自己判（_char_src + _one_char_rx），不手写折叠：(?i)[A-Z] 也吃 a、ẞ 和 ß 互认、× 在 [À-Þ] 中间。
    配 700 个字（同写作台），例子本身得是慢的；打回的话和别的「挨在一起」一样。"""
    t0 = time.perf_counter()
    re.search(pattern, ch * 700)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的，否则测不出东西"
    with pytest.raises(R.RuleOutputError) as ei:
        R.validate_code(_code(pattern))
    assert "挨在一起" in str(ei.value)


def _adjacent_gate_refuses(pattern: str) -> bool:
    """只跑「挨在一起」那一道（写作台 validator.assert_regex_is_safe 里 _check_adjacent_repeats 那一步）：写作台的这几张用例表
    直接调 validator，不过形状那道；这里对的是同一道。"""
    try:
        R._check_adjacent_repeats(R._regex_tree(pattern))
    except R._AmbiguousRepeats:
        return True
    return False


def _refusal(pattern: str) -> str:
    with pytest.raises(R.RuleOutputError) as ei:
        R.validate_code(_code(pattern))
    return str(ei.value)


@pytest.mark.parametrize("pattern, ch", DESK_LOOKAHEAD_POSSESSIVE_SLOW)
def test_lookaheads_possessives_and_atomic_groups_do_not_separate(pattern, ch):
    """前瞻里的量词和前面开着的照样挨着；占有量词 / 原子组能吃空、或者吃的是前面开着的也能吃的字，前后照样挨着（同写作台）。
    配 1000 个字（同写作台），例子本身得是慢的；形状那道都过，打回的话说「挨在一起」。"""
    t0 = time.perf_counter()
    re.search(pattern, ch * 1000)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的，否则测不出东西"
    assert _adjacent_gate_refuses(pattern)
    assert "挨在一起" in _refusal(pattern)


@pytest.mark.parametrize("pattern, ch, n", DESK_OPTIONAL_GROUP_SLOW)
def test_repeats_inside_optional_or_counted_groups_still_meet_their_neighbours(pattern, ch, n):
    """(?:…)? / (?:…){m,n} 不是一整块：组里的量词接着组前面开着的查，组里走完还开着的往后传（同写作台）。
    形状那道也拒的（量词套了带量词 / 分支的组）先说形状；其余说「挨在一起」。"""
    t0 = time.perf_counter()
    re.search(pattern, ch * n)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的，否则测不出东西"
    assert _adjacent_gate_refuses(pattern)
    msg = _refusal(pattern)
    assert ("挨在一起" if R._regex_shape_ok(R._regex_tree(pattern)) else "套在") in msg, (pattern, msg)


@pytest.mark.parametrize("pattern, texts", DESK_LOOKAHEAD_POSSESSIVE_FAST + DESK_OPTIONAL_GROUP_FAST,
                         ids=lambda v: v if isinstance(v, str) else None)
def test_lookaheads_possessives_and_optional_groups_that_stay_allowed_are_fast(pattern, texts):
    """「挨在一起」那道放行的（\\d+(?=\\s*元)、a*+a*b、\\d+(?:\\.\\d+)?元 …），在写作台掐表用的正文上真的快（2000 字以内）。
    形状那道拒的（^\\S+(?:\\s+\\S+){3}）整体还是打回，同写作台 normalize_check。"""
    assert not _adjacent_gate_refuses(pattern)
    if pattern in DESK_VALIDATOR_ONLY:
        assert "套在" in _refusal(pattern)
    else:
        assert R.validate_code(_code(pattern))["pattern"] == pattern
    for text in texts:
        t0 = time.perf_counter()
        re.search(pattern, text)
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])


def test_the_check_computes_each_char_set_once(monkeypatch):
    """前瞻 / 原子组 / 占有量词里那一段接着前面开着的查，同一个单字节点（连同生效的旗）一次检查里只算一次字集（同写作台
    test_the_check_computes_each_char_set_once）：不然 .*(?=(?:(?:(?:a\\d){16}){16}){10}) 闸自己就要 0.4~0.6 秒。
    这三条形状那道先拒（定次量词套了三层），所以直接调 _check_adjacent_repeats。"""
    calls, real = [], R._char_test
    monkeypatch.setattr(R, "_char_test", lambda *a: calls.append(repr(a)) or real(*a))
    for pattern in (".*(?=(?:(?:(?:a\\d){16}){16}){10})", ".*(?!(?:(?:(?:a\\d){16}){16}){10})x",
                    ".*(?>(?:(?:(?:a\\d){16}){16}){10})x"):
        calls.clear()
        t0 = time.perf_counter()
        R._check_adjacent_repeats(R._regex_tree(pattern))
        assert time.perf_counter() - t0 < 0.2, pattern
        assert len(calls) == len(set(calls)), (pattern, len(calls), len(set(calls)))
        assert "套在" in _refusal(pattern)


@pytest.mark.parametrize("pattern", DESK_CONDITIONAL)
def test_conditional_groups_are_refused_with_their_own_reason(pattern):
    """条件组 (?(1)是|否) 几道闸都看不进它里面（同写作台 validator._has_conditional）：先查、单独说是条件组的事。"""
    with pytest.raises(R.RuleOutputError) as ei:
        R.validate_code(_code(pattern))
    assert "条件组" in str(ei.value)


def test_conditional_groups_are_named_even_without_the_parse_tree(monkeypatch):
    """拿不到解析树时照写作台按原文里的「(?(」认条件组，打回的话说条件组；别的照旧说拿不到解析树。"""
    monkeypatch.setattr(R, "_regex_tree", lambda p: None)
    with pytest.raises(R.RuleOutputError) as ei:
        R.validate_code(_code("()(?(1)a|b)"))
    assert "条件组" in str(ei.value)
    assert R._regex_problem("最") is not None and "解析树" in R._regex_problem("最")


def test_scoped_flags_follow_the_group_not_the_whole_pattern():
    """_scoped：组里加的旗只在组里生效、组里去掉的只在组里去掉；(?a:…) / (?u:…) 换掉外面的字符类旗，不叠加。"""
    u, a, i, s = re.UNICODE, re.ASCII, re.IGNORECASE, re.DOTALL

    def group_av(p):
        node = R._regex_tree(p)[0]
        assert getattr(node[0], "name", str(node[0])) == "SUBPATTERN"
        return node[1]

    assert R._scoped(u, group_av("(?i:a)")) == u | i
    assert R._scoped(u | i, group_av("(?-i:a)")) == u
    assert R._scoped(u, group_av("(?s:a)")) == u | s
    assert R._scoped(u, group_av("(?a:a)")) == a
    assert R._scoped(a | i, group_av("(?u:a)")) == u | i
    assert R._scoped(u | i, group_av("(a)")) == u | i                   # 不带旗的组：照外面的（(?:a) 解析时就摊平了，没有组）


# 锚点（\b \B ^ $）零宽、只有一种匹配法：写作台 rulecheck 把它们单列在 _ZERO_WIDTH_OPS 里，_fixed_simple 和单个字符一样认，
# 量词套的组里带锚点照放行；组里另有量词或分支的照拒。合成的例子，2026-10-20 在写作台 2eef1a6 上逐条核过结论。
ANCHOR_IN_REPEATED_GROUP_PASS = ["(?:\\b品牌)+", "(?:^a)+x", "(?:a$){2}", "(?:\\bab\\b){3,}", "(?:\\Ba)+"]
ANCHOR_IN_REPEATED_GROUP_REJECT = ["(?:\\b(?:ab)+)+", "(?:a\\b|b)+"]


def test_anchors_in_a_repeated_group_count_as_fixed_width_like_the_desk():
    """op 名分组同写作台 rulecheck（_SINGLE_CHAR_OPS 不含 AT，AT 在 _ZERO_WIDTH_OPS），_fixed_simple 两组都认。"""
    assert R._ZERO_WIDTH_OPS == {"AT"} and R._SINGLE_CHAR_OPS.isdisjoint(R._ZERO_WIDTH_OPS)
    for p in ANCHOR_IN_REPEATED_GROUP_PASS:
        assert R.validate_code(_code(p))["pattern"] == p
    for p in ANCHOR_IN_REPEATED_GROUP_REJECT:
        with pytest.raises(R.RuleOutputError) as ei:
            R.validate_code(_code(p))
        assert "套在" in str(ei.value), p


def test_every_regex_the_prompt_teaches_is_one_the_desk_records():
    """PROMPT_HEAD 教给模型的正则（标签个数、极限词带上下文、「最多出现 N 次」展开写）都要在写作台放行的清单里：
    以后改 prompt 教了新写法，先去写作台确认收得下，再把它加进上面的清单。"""
    taught = [json.loads(ln.strip().split("   ")[0])["pattern"] for ln in R.PROMPT_HEAD.splitlines()
              if ln.strip().startswith('{"kind":"forbidden_regex"')]
    taught = [p for p in taught if p != "正则"]                                   # 第一行是格式说明里的占位
    assert len(taught) == 3
    for p in taught:
        assert p in DESK_MUST_PASS, p
        R.validate_code(_code(p))


def test_adjacent_repeats_get_a_reason_the_model_can_act_on():
    """形状那道放行、挨在一起那道拒收的：打回的话要说清是什么问题、怎么改（重试时原样回给模型）。"""
    for p in (".*.*x", "a.*a.*a.*b", "!.*!.*!$", "\\w*\\w*\\d+", "[!！]" + "[\\s\\S]*[!！]" * 15, "a.*?[!！].*[!！][!！]"):
        with pytest.raises(R.RuleOutputError) as ei:
            R.validate_code(_code(p))
        msg = str(ei.value)
        assert "挨在一起" in msg and "X[\\s\\S]*X" in msg and f"{R._MAX_COUNT_CHAIN} 份" in msg, (p, msg)


def test_a_check_that_would_take_too_long_to_analyse_is_refused_quickly():
    """形状那道放行、但闸自己要走很多步的（100 个互不相交的 X? 后面接一个展开 16 份的定长组）：按太复杂打回，同写作台；
    定次量词套五层的那条形状那道就挡了。两条都不能把闸自己拖住。"""
    cjk = "".join(chr(0x4E00 + 7 * k) for k in range(100))
    too_complex = "".join(c + "?" for c in cjk) + "(?:" + "\\S" * 10 + "){16}"
    assert len(too_complex) <= R.MAX_PATTERN_CHARS
    for p, words in ((too_complex, "太复杂"), ("(?:(?:(?:(?:(?:a{16}){16}){16}){16}){16})", "套在")):
        t0 = time.perf_counter()
        with pytest.raises(R.RuleOutputError) as ei:
            R.validate_code(_code(p))
        assert time.perf_counter() - t0 < 1
        assert words in str(ei.value)


def test_checking_both_lists_is_itself_fast():
    t0 = time.perf_counter()
    for p in DESK_MUST_REJECT + DESK_MUST_PASS:
        try:
            R.validate_code(_code(p))
        except R.RuleOutputError:
            pass
    assert time.perf_counter() - t0 < 2


def test_without_the_parse_tree_no_regex_is_compiled(monkeypatch):
    """拿不到 re 的解析树时写作台（rulecheck._assert_regex_shape）一条正则都不收；这里也一条都不放，
    不再退回文字形状的兜底（它会放行 .*.*x 这类写作台拒收的）。别的 kind 不受影响。"""
    monkeypatch.setattr(R, "_regex_tree", lambda p: None)
    for p in ("最", ".*.*x", "品牌[\\s\\S]*品牌[\\s\\S]*品牌"):
        with pytest.raises(R.RuleOutputError) as ei:
            R.validate_code(_code(p))
        assert "解析树" in str(ei.value)
    assert R.validate_code({"kind": "forbidden_word", "words": ["最便宜"]})["words"] == ["最便宜"]


def test_the_parse_tree_is_actually_available_on_this_python():
    """上面那条是降级路径；主路径（re._parser / sre_parse）断了的话所有 forbidden_regex 都编不出来，要有人知道。"""
    assert R._regex_tree("(a+)+$") is not None


def test_a_refused_regex_is_retried_with_the_reason_then_kept_per_rule():
    """整条链路：模型第一次给了写作台拒收的 .*.*x，带着原因重编一次；第二次给对了就收下，还不对只让那一条带 error、不拖累同批。"""
    rules = [{"id": "r1", "text": "合成规则一", "severity": "soft"}, {"id": "r2", "text": "合成规则二", "severity": "soft"}]
    good = {"kind": "forbidden_regex", "pattern": "最[\\s\\S]*最[\\s\\S]*最", "where": ["body"]}

    def result(rid, pattern):
        return {"id": rid, "verdict": "code", "code": dict(good, pattern=pattern), "questions": [], "coverage": "full",
                "uncovered": "", "why": "合成"}

    outputs = [[result("r1", ".*.*x"), result("r2", "a*a*a*a*b")], [result("r1", good["pattern"]), result("r2", ".*.*b")]]
    prompts = []

    def gen(prompt, n=1):
        prompts.append(prompt)
        return [json.dumps({"results": outputs.pop(0)}, ensure_ascii=False)]

    got = {r["id"]: r for r in R.compile_batch(gen, rules)}
    assert len(prompts) == 2 and "挨在一起" in prompts[1]
    assert got["r1"]["code"] == good and not got["r1"].get("error")
    assert got["r2"]["code"] is None and "挨在一起" in got["r2"]["error"]


# ── 放行的真的快：最坏的正文（2 万字，即写作台的执行上限 validator.MAX_REGEX_TARGET_CHARS）掐表 ─────────────────────────
# 写作台 test_what_the_relaxed_rules_let_through_is_actually_fast 的正文原样抄过来，再加几种冲着 PROMPT_HEAD 教的写法去的。
MAX_TARGET_CHARS = 20_000
_WORST_TEXTS = {
    "count": ["!" * 3 + "a" * 20_000, "！" * 20_000, "最" * 2 + "\n" * 20_000, "#" * 5 + " " * 20_000,
              "\n" + " " * 20_000, "\n \n a" * 3000, "哦" * 2 + "x" * 20_000],
    "tail": ["1" * 20_000, "1 " * 10_000, "1" + " " * 20_000 + "a", "!" * 20_000, "! " * 10_000, "a!" * 10_000],
    "jev": ["品牌" * 10_000, "品牌" * 2 + "x" * 20_000, "最" * 20_000, "#a " * 7_000, "a" * 20_000, "哈" * 19_999 + "。",
            "\n" * 20_000, " " * 20_000, "S " * 10_000],
}
_ALL_WORST = [t[:MAX_TARGET_CHARS] for texts in _WORST_TEXTS.values() for t in texts]

# 两边都放行、在 2 万字的一行上却是 n² 的（re.search 每个起点都重来一遍，开头是没有固定前缀的可变长量词，每一遍又是 O(n)）：
# 2026-10-20 在 Python 3.13 上实测 .*[!！]{2,} 配「原价」+ 2 万个 x 3.6–4.6 秒，a*b*c 配 2 万个 a 3 秒，.*私信.* 配 2 万个「私」2.1 秒，
# [a-z]+@[a-z]+\.com 1.3 秒，\d+% / \d+\.\d+ 配 2 万个 1 0.6–0.9 秒，a*+a*b 0.2–0.25 秒。
# 这一类写作台的闸（挨在一起那道）不管，不是两边不一样；要挡得先改写作台，所以这里不单独加严，只是不拿它们掐表。
# 写作台 codex review on aw#115 那一轮（2eef1a6）新放行的三条也是这一类（不是教的写法）：(?i)(?-i:a*)(?-i:A*)B 配 "!!!" + 2 万个 a 4.4 秒，
# (?a)(?u:\W*)é*x 配换行 + 2 万个空格 4.9 秒，.*\n*x 配 2 万个 ! 3.3 秒（2026-10-20，Python 3.13）。
# a*+a*b 在 3.13 上 0.2–0.25 秒、3.11 上 0.07 秒，压线，按慢的那边留在这里。
# lookahead/possessive 那一轮写作台新放行的 18 条里 15 条也是这一类（写作台只拿 2000 字以内的正文掐表，见上面
# DESK_LOOKAHEAD_POSSESSIVE_FAST / DESK_OPTIONAL_GROUP_FAST）。2026-10-20 在这里的 2 万字最坏正文上实测（3.13 / 3.11，取最慢的一条正文）：
# \d+(?=元) 2.6 / 3.1 秒，\d+(?=\s*元) 4.3 / 6.0，(?=.*品牌).*折扣 0.7 / 0.8，(?=.*A)(?=.*B).*C 0.7 / 0.6，\w+(?=\s) 6.0 / 5.7，
# [^\n]*(?=\n) 3.0 / 3.2，\d+(?<=\d)x 3.7 / 4.4，\d++\.\d+ 0.56 / 0.56，(?>\w+)\s+\w+ 1.4 / 1.2，.*\d++ 3.7 / 4.3，
# \d+(?:\.\d+)?(?=\s*元) 8.4 / 10.2，\d+(?:\.\d+)?(?=\s*[元块]) 10.5 / 12.9，\d+(?:\.\d+)?\s*[元块] 10.4 / 10.5，
# \d+(?:\.\d+)?元 6.3 / 7.1，[\u4e00-\u9fff]+(?:[a-z]+)?！ 9.6 / 10.1。快的三条 \d+(?!\d)、(?<!\d)\d{11}(?!\d)、\d+(?=\d*) 都在
# 1 毫秒以内，照常掐表。打回的话（同写作台 UnsafeRegex 那句）教的 \d+(?:[,，]\d+)?元 也是这一类（3.13 上 2 万个 1 6.7 秒），
# 不在写作台的用例表里，所以不在这张单子上；两边都放行，对拍探针里有它。
KNOWN_QUADRATIC = [".*[!！]{2,}", "a*b*c", ".*私信.*", "[a-z]+@[a-z]+\\.com", "\\d+%", "\\d+\\.\\d+", "a*+a*b",
                   "(?i)(?-i:a*)(?-i:A*)B", "(?a)(?u:\\W*)é*x", ".*\\n*x",
                   "\\d+(?=元)", "\\d+(?=\\s*元)", "(?=.*品牌).*折扣", "(?=.*A)(?=.*B).*C", "\\w+(?=\\s)", "[^\\n]*(?=\\n)",
                   "\\d+(?<=\\d)x", "\\d++\\.\\d+", "(?>\\w+)\\s+\\w+", ".*\\d++", "\\d+(?:\\.\\d+)?(?=\\s*元)",
                   "\\d+(?:\\.\\d+)?(?=\\s*[元块])", "\\d+(?:\\.\\d+)?\\s*[元块]", "\\d+(?:\\.\\d+)?元",
                   "[\\u4e00-\\u9fff]+(?:[a-z]+)?！"]


def test_the_known_quadratic_ones_are_still_on_the_pass_list():
    assert set(KNOWN_QUADRATIC) <= set(DESK_MUST_PASS), "清单换了：KNOWN_QUADRATIC 跟着改"


@pytest.mark.parametrize("pattern", [p for p in DESK_MUST_PASS if p not in KNOWN_QUADRATIC])
def test_what_compiles_is_actually_fast_on_the_worst_texts(pattern):
    R.validate_code(_code(pattern))
    rx = re.compile(pattern)
    for text in _ALL_WORST:
        t0 = time.perf_counter()
        rx.search(text)
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])


@pytest.mark.parametrize("pattern, texts", [               # 写作台那张表原样：放宽的几种配各自最坏的正文
    ("[!！][\\s\\S]*[!！][\\s\\S]*[!！][\\s\\S]*[!！]", "count"),
    ("最[\\s\\S]*最[\\s\\S]*最", "count"),
    ("哦[\\s\\S]*哦[\\s\\S]*哦", "count"),
    ("#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#", "count"),
    ("\\n\\s*\\n\\s*\\n", "count"),
    ("\\d+\\s*\\d+", "tail"),
    ("[!！]+\\s*[!！]+", "tail"),
])
def test_what_the_desk_relaxed_is_fast_as_in_the_desk_test(pattern, texts):
    R.validate_code(_code(pattern))
    for text in _WORST_TEXTS[texts]:
        t0 = time.perf_counter()
        re.search(pattern, text[:MAX_TARGET_CHARS])
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])
