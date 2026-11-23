# -*- coding: utf-8 -*-
"""Synthetic regression tests for JevforCoentent audit hardening fixes."""
from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_fill_text_single_pass_and_literal_braces_in_post_body():
    from judge import banks as B

    # Single-pass: replacement value containing another placeholder must NOT expand
    filled = B.fill_text("{a} / {b}", {"a": "{b}", "b": "VAL"})
    assert filled == "{b} / VAL"

    # Literal braces inside fill values must not trigger unfilled() false positives
    amb = {"choice_top_min": 0.6, "choice_margin_min": 0.15, "noul_band": [0.4, 0.6]}
    q = B.Question(id="q1", jtype="noul", instructions="帖子正文：{post_body}\n请判断", criteria={"true": "是", "false": "否"})
    bank = B.Bank(name="test_bank", version="0.1", model="m", fmt="jev", questions=[q], ambiguity=amb, sha256="x")
    assert B.unfilled(bank, {"post_body": "示例文本包含 {price} 和 {ingredient} 花括号"}) == []
    assert B.unfilled(bank, {}) == ["q1"]


def test_comment_api_accepts_literal_braces_in_post_body(monkeypatch):
    from judge import api as A

    monkeypatch.setenv("JUDGE_MOCK", "1")
    monkeypatch.setenv("JUDGE_API_KEY", "admin-k")
    monkeypatch.setenv("JUDGE_COMMENT_TOOLS_ENABLED", "1")
    c = TestClient(A.app)
    headers = {"X-Judge-Key": "admin-k"}

    body_with_braces = "合成测试正文：预算 {price_range} 元，使用 {template_a} 方案对比。"
    r1 = c.post(
        "/comment_repair_plan_for",
        json={
            "post_title": "测试标题",
            "post_body": body_with_braces,
            "text": "我也用过这个方案，对比下来确实稳一些",
            "slot": {"id": "s1", "speech_act": ["补充经验"]},
            "project": "TUGE",
        },
        headers=headers,
    )
    assert r1.status_code == 200, r1.text

    r2 = c.post(
        "/judge_thread",
        json={
            "post_title": "测试标题",
            "post_body": body_with_braces,
            "comments": [{"text": "这条怎么选？", "role": "读者位"}],
            "project": "TUGE",
        },
        headers=headers,
    )
    assert r2.status_code == 200, r2.text


def test_banks_interpret_guards_choice_vs_noul():
    from judge import banks as B

    amb = {"choice_top_min": 0.6, "choice_margin_min": 0.15, "noul_band": [0.4, 0.6]}
    q_choice = B.Question(id="q_c", jtype="choice", instructions="pick", criteria={"A": "opt A", "B": "opt B"})
    bank_c = B.Bank(name="b_c", version="0.1", model="m", fmt="jev", questions=[q_choice], ambiguity=amb, sha256="x")
    # Choice question receiving a mismatched noul payload must NOT be coerced to '是'
    out_mismatch = B.interpret(bank_c, {"answers": {"q_c": {"type": "noul", "noul": 0.9}}})
    assert out_mismatch["q_c"]["answer"] is None
    assert out_mismatch["q_c"]["invalid_reason"] == B.INVALID_OUT_OF_VOCAB

    q_noul = B.Question(id="q_n", jtype="noul", instructions="yes/no", criteria={"true": "t", "false": "f"})
    bank_n = B.Bank(name="b_n", version="0.1", model="m", fmt="jev", questions=[q_noul], ambiguity=amb, sha256="x")
    out_n = B.interpret(bank_n, {"answers": {"q_n": {"type": "noul", "noul": 0.9}}})
    assert out_n["q_n"]["answer"] == "是"
    # Noul question receiving a mismatched choice payload must be out_of_vocab
    out_n_bad = B.interpret(bank_n, {"answers": {"q_n": {"choice": "true", "probabilities": {"true": 0.9}}}})
    assert out_n_bad["q_n"]["answer"] is None
    assert out_n_bad["q_n"]["invalid_reason"] == B.INVALID_OUT_OF_VOCAB


def test_apply_evidence_rejects_malformed_answers_bool_and_nan():
    from judge import core as C

    sent_map = {"q1": ["第一句证据。"]}

    # Non-dict answer entry does not crash
    items1 = {"q1": {"answer": "是"}}
    C.apply_evidence(items1, {"answers": {"q1": "not-a-dict"}}, sent_map)
    assert "evidence" not in items1["q1"]

    # Boolean choice -> NOT coerced via int(True) == 1
    items2 = {"q1": {"answer": "是"}}
    C.apply_evidence(items2, {"answers": {"q1": {"choice": True, "probabilities": {True: 0.9}}}}, sent_map)
    assert items2["q1"]["evidence"] is None
    assert items2["q1"]["invalid_reason"] == C.INVALID_EVIDENCE_NOT_FOUND

    # NaN probability -> safely falls back to 0.0 (no NaN JSON break)
    items3 = {"q1": {"answer": "是"}}
    C.apply_evidence(items3, {"answers": {"q1": {"choice": "1", "probabilities": {"1": float("nan")}}}}, sent_map)
    assert items3["q1"]["evidence"] == "第一句证据。"
    assert items3["q1"]["evidence_p"] == 0.0


def test_repair_plan_noul_target_definition_lookup():
    from judge import banks as B, loop as L

    amb = {"choice_top_min": 0.6, "choice_margin_min": 0.15, "noul_band": [0.4, 0.6]}
    q = B.Question(
        id="has_hook",
        jtype="noul",
        instructions="是否有钩子？",
        criteria={"true": "开头具备吸引点", "false": "开头平铺直叙"},
    )
    bank = B.Bank(name="project", version="0.1", model="m", fmt="jev", questions=[q], ambiguity=amb, sha256="x")
    dj = L.DraftJudgement(
        profile={"has_hook": "否"},
        detail={"project": {"has_hook": {"answer": "否", "p": 0.9}}},
        wants={("project", "has_hook"): "是"},
        hard_fails=[("project", "has_hook", "否", 0.9, None)],
        calls=1,
    )
    plan = L.repair_plan(dj, {"project": bank}, target={"has_hook": "是"})
    assert len(plan) == 2
    assert all(p["want"] == "是" and p["definition"] == "开头具备吸引点" for p in plan)


def test_hidden_config_concurrent_and_empty_env(tmp_path, monkeypatch):
    from judge import hidden as H

    cfg_path = tmp_path / "hidden.yaml"
    cfg_path.write_text(
        "banks:\n  human_feel_para:\n    fraction: 0.3333\n    override: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(H, "CONFIG", cfg_path)
    with H._lock:
        H._cache.clear()
    monkeypatch.setenv("JUDGE_TZ", "")

    def _call(_i):
        return H.hidden_ids("human_feel_para_v0.2", ["q1", "q2", "q3", "q4", "q5", "q6"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_call, range(16)))
    assert all(len(r) == 2 and r == results[0] for r in results)


def test_empty_env_base_urls_fall_back_to_defaults(monkeypatch):
    from judge import external as E, jev_client as JC, loop as L

    monkeypatch.setenv("TYPESAFE_BASE_URL", "   ")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    client = JC.JevClient(mock=False)
    assert client.base_url == JC.DEFAULT_BASE_URL

    monkeypatch.setenv("TIKHUB_BASE_URL", "  ")
    assert E._tikhub_base() == "https://api.tikhub.io"
    monkeypatch.setenv("TIKHUB_BASE_URL", "https://custom.tikhub.example/")
    assert E._tikhub_base() == "https://custom.tikhub.example"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", " ")
    gen = L.AnthropicCompatGenerator(model="test-model", api_key="k")
    assert gen.base_url == "https://api.anthropic.com"


def test_mcp_draft_body_preserves_explicit_empty_banks():
    from judge import mcp_server as M

    body = M._draft_body("t", "b", [], "on_fail", "TUGE", None, None, None, None, None, "draft")
    assert body["banks"] == []


def test_mcp_local_comment_repair_plan_needs_review_not_passed(monkeypatch):
    from judge import comments as CM, mcp_server as M

    monkeypatch.setenv("JUDGE_ALLOW_LOCAL_JEV", "1")
    monkeypatch.setenv("JUDGE_MOCK", "1")
    fake_cj = SimpleNamespace(
        passed=lambda: True,
        needs_review=True,
        flags=["needs_review_flag"],
        hard_fails=[],
        profile=lambda: {},
        calls=1,
    )
    monkeypatch.setattr(CM, "judge_comment", lambda *a, **k: fake_cj)
    monkeypatch.setattr(CM, "comment_repair_plan", lambda *a, **k: [])
    res = M.comment_repair_plan_for(
        "标题", "合成测试正文内容足够长", "不错", {"id": "s1", "speech_act": ["补充经验"]}, project="TUGE"
    )
    assert res["needs_review"] is True
    assert res["passed"] is False


def test_judge_draft_hard_rules_hides_hidden_qids_and_blocks_oracle(monkeypatch):
    from judge import api as A

    monkeypatch.setenv("JUDGE_MOCK", "1")
    monkeypatch.setenv("JUDGE_API_KEY", "admin-k")
    monkeypatch.setenv("JUDGE_WRITER_API_KEYS", "writer-k")
    monkeypatch.setattr(A, "all_hidden", lambda banks: {"para_register", "para_friction"})

    c = TestClient(A.app)
    headers = {"X-Judge-Key": "writer-k"}
    draft_body = "合成测试正文：第一段描述真实使用场景，第二段补充细节感受与对比判断，字数足够长。"

    # Unknown qid must NOT dump hidden qids in 422 message
    r_unk = c.post(
        "/judge_draft",
        json={
            "title": "t",
            "body": draft_body,
            "project": "TUGE",
            "hard_rules": {"human_feel_para_v0.2:no_such_qid": True},
        },
        headers=headers,
    )
    assert r_unk.status_code == 422
    assert "para_register" not in r_unk.text
    assert "para_friction" not in r_unk.text

    # Probing a hidden qid must return 422 identically (no 200 quarter-oracle)
    r_hid = c.post(
        "/judge_draft",
        json={
            "title": "t",
            "body": draft_body,
            "project": "TUGE",
            "hard_rules": {"human_feel_para_v0.2:para_register": False},
        },
        headers=headers,
    )
    assert r_hid.status_code == 422
    assert "para_friction" not in r_hid.text


def test_judge_rejects_unknown_subject_type_before_policy_bypass(monkeypatch):
    from judge import api as A

    monkeypatch.setenv("JUDGE_MOCK", "1")
    monkeypatch.setenv("JUDGE_API_KEY", "admin-k")
    c = TestClient(A.app)
    headers = {"X-Judge-Key": "admin-k"}

    # Typo subject_type ('draft' / 'aw_versions') must 422 instead of being treated as published=True
    for bad_type in ("draft", "aw_versions", "unknown"):
        r = c.post(
            "/judge",
            json={
                "bank": "platform_health_v0.1",
                "subjects": [{"subject_type": bad_type, "subject_id": "s1", "state": {"正文": "测试"}}],
                "project": "OKMAN",
                "category": "处方药",
            },
            headers=headers,
        )
        assert r.status_code == 422, (bad_type, r.status_code, r.text)
        assert "subject_type" in r.text
