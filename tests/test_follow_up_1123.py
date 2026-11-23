# -*- coding: utf-8 -*-
"""11-23 第二轮跟进（truth-vault S-20 ④⑥、W-56）。全合成样本，零付费调用。"""
from __future__ import annotations

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
    """W-56：「如果…要…」的规则一律问「条件出现了但要求没做到」，pass_if 否。"""
    from judge import rules as R

    head = R.PROMPT_HEAD
    assert "如果 / 若 / 当……时，要……" in head
    assert "正文结尾有没有向读者提问，但问的不是读者自己的经历或做法？" in head
    assert "pass_if 一定写「否」" in head


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
