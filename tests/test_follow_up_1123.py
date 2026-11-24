# -*- coding: utf-8 -*-
"""11-23 第二轮跟进（truth-vault S-20 ④⑥⑦、W-56）。全合成样本，零付费调用。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_vendored_feature_bank_digest_is_pinned_to_truth_vault():
    """G-24 / S-20 ⑥：只同步 YAML、不同步 tv_feature_bank.py 或 SHA256SUMS 时这里要红。
    摘要与 TV 侧 feature_questions_v0_1 的规范化摘要一致（TV 那边记的前 12 位 ba0f570c47d5）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("tv_feature_bank_vendored", ROOT / "banks" / "vendor" / "tv_feature_bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = (ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml").read_bytes()
    assert mod.bank_digest(raw) == "ba0f570c47d594ca4dad571c5a9f5d06da51861b041ba364c3a915220b985ddd"


def test_compile_prompt_tells_conditional_rules_to_ask_the_violation():
    """W-56：「如果…要…」的规则一律问「条件出现了但要求没做到」，pass_if 否；没碰到条件的稿子算守住。

    示例是合成的格式规则（操作步骤要编号），和 W-56 正在复核的那条「怎么结尾」的写法规则无关：
    说明里要是直接给出那条规则的目标问法，重编之后模型照抄，就看不出这段通用说明对别的带条件规则管不管用。"""
    from judge import rules as R

    head = R.PROMPT_HEAD
    assert "如果 / 若 / 当……时，要……" in head
    assert "一律问规则有没有被违反，不问要求做没做到" in head
    assert "pass_if 一定写「否」" in head
    assert "没碰到条件的稿子自动答「否」、算守住" in head
    assert "正文有没有列出操作步骤，却没有给每一步编号？" in head
    assert "不要问「正文的操作步骤有没有编号？」再写 pass_if 是" in head
    assert "结尾" not in head   # 编题说明里不放任何「怎么结尾」的例子


class _YesJev:
    """每道题都答「是」(noul 0.95)，不给证据；记下调用次数。"""

    mock = True

    def __init__(self):
        self.calls = 0

    def call(self, body):
        self.calls += 1
        return {"answers": {qid: {"type": "noul", "noul": 0.95} for qid in body["questions"]}, "usage": {}}


def test_judge_endpoint_enforces_evidence_on_jev_format_banks():
    """S-20 ④ / AU-F03：POST /judge 走 Jev 格式题库（platform_health_v0.1，evidence: on_true）时，
    答「是」却没有证据的题标 evidence_not_found，和 judge_note、/judge_draft 一个口径；不多调一次 Jev。"""
    pytest.importorskip("fastapi")
    from judge import api as A

    bank = A.get_bank("platform_health_v0.1")
    s = A.Subject(subject_type="note", subject_id="synthetic-1", state={"正文": "合成测试正文"})
    jev = _YesJev()
    r = A._judge_one(jev, bank, s, with_evidence=True)
    assert jev.calls == 1
    assert r.items and all(it["invalid_reason"] == "evidence_not_found" for it in r.items.values())
    rows = A.ledger_rows(r, bank)
    assert rows and all(row["answer"] is None and row["prob"] is None for row in rows)


def test_judge_endpoint_leaves_evidence_free_jev_banks_alone():
    """不要求证据的 Jev 格式题库（题目没有 evidence: on_true）答案照旧。"""
    pytest.importorskip("fastapi")
    from judge import api as A
    from judge import banks as B

    amb = {"choice_top_min": 0.6, "choice_margin_min": 0.15, "noul_band": [0.4, 0.6]}
    q = B.Question(id="q1", jtype="noul", instructions="合成题？", criteria={"true": "是", "false": "否"})
    bank = B.Bank(name="synthetic_jev", version="0.1", model="m", fmt="jev", questions=[q], ambiguity=amb, sha256="x")
    s = A.Subject(subject_type="note", subject_id="synthetic-2", state={"正文": "合成"})
    r = A._judge_one(_YesJev(), bank, s, with_evidence=True)
    assert r.items["q1"]["answer"] == "是" and not r.items["q1"].get("invalid_reason")


# 代码里读环境变量的几种写法：os.environ.get / os.environ[...] / 各模块自己的 _env / _num / _env_int / _env_number
_ENV_READ = re.compile(r"""(?:environ\.get|environ\[|getenv|_env|_num|_env_int|_env_number)\(?\s*['"]([A-Z][A-Z0-9_]+)['"]""")


def _env_example() -> str:
    return (ROOT / ".env.example").read_text(encoding="utf-8")


def test_env_example_lists_every_env_var_the_service_reads():
    """S-20 ⑦：judge/ 里代码实际读的环境变量，.env.example 一个都不漏（JEV_TIMEOUT_SEC / JEV_RETRIES / ANTHROPIC_CUSTOM_HEADERS 曾经漏过）；
    新补的几行只写名字和说明，不带值。"""
    read = set()
    for p in (ROOT / "judge").glob("*.py"):
        read.update(_ENV_READ.findall(p.read_text(encoding="utf-8")))
    assert {"JEV_TIMEOUT_SEC", "JEV_RETRIES", "ANTHROPIC_CUSTOM_HEADERS", "JUDGE_ALLOW_LOCAL_JEV"} <= read
    env = _env_example()
    declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", env, re.M))
    assert sorted(read - declared) == []
    for name in ("JEV_TIMEOUT_SEC", "JEV_RETRIES", "ANTHROPIC_CUSTOM_HEADERS"):
        assert re.search(rf"^# {name}=\s+# ", env, re.M), name


def test_env_example_local_jev_comment_matches_what_mcp_server_gates():
    """JUDGE_ALLOW_LOCAL_JEV 的说明照代码写：只管 judge.mcp_server 里走本机 Jev 的那几个评论工具，其余工具始终走 HTTP。"""
    src = (ROOT / "judge" / "mcp_server.py").read_text(encoding="utf-8")
    gated = re.findall(r'_require_local_jev\("(\w+)"\)', src)
    assert sorted(gated) == ["comment_repair_plan_for", "judge_comments", "judge_thread"]
    assert 'os.environ.get("JUDGE_ALLOW_LOCAL_JEV") != "1"' in src
    env = _env_example()
    block = env[env.index("# JUDGE_ALLOW_LOCAL_JEV="):]
    assert "只有等于 1 时" in block
    for tool in gated:
        assert tool in block, tool
    assert "judge_draft / repair_plan_for / list_banks" in block and "始终走 HTTP 服务" in block
