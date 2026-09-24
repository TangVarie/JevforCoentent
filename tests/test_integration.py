# -*- coding: utf-8 -*-
"""第二批：设计缺口（闸二门禁、暗题、数据出境、摩擦换位、每题留出口、Mode A 卫生）、TV 答题契约对齐、零碎修复。全部 mock，不联网。"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import core as C  # noqa: E402
from judge import spans as S  # noqa: E402
from judge.jev_client import RETRY_STATUSES, JevClient, mock_response  # noqa: E402

FQ = ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml"
PH = ROOT / "banks" / "platform_health_v0.1.yaml"
HF2 = ROOT / "banks" / "human_feel_para_v0.2.yaml"
BODY = "上周办了张健身卡。\n教练问我是不是抽烟。\n办卡花了三千多，挺亏的。\n你们是先戒烟还是边练边戒？"


class FixedJev:
    """按题号给固定答案；没列的题不返回（测 missing）。"""

    def __init__(self, answers: dict, mock: bool = False):
        self.answers, self.calls, self.mock = answers, 0, mock

    def call(self, body):
        self.calls += 1
        qs = body["questions"]
        if all(set(q["criteria"]) - {"没有"} <= {str(i) for i in range(1, 300)} for q in qs.values()):   # 证据选句
            return {"answers": {q: {"type": "choice", "choice": "1", "probabilities": {"1": 0.9}} for q in qs}, "usage": {"input_tokens": 2}}
        return {"answers": {q: a for q, a in self.answers.items() if q in qs}, "usage": {"input_tokens": 1}}


# ── TV 答题契约 ──

def test_askable_matches_tv_render_call_skip_rules():
    b = B.load_bank(FQ)
    tv = S._tv
    for raw, mode, title_col in [("标题：老公戒烟成功但开始疯狂吃零食了\n正文：老公戒烟到今天正好40天了，烟是真的没抽。", "markers", None),
                                 ("【标题】好\n【正文】短短的正文十二个字而已哦", "markers", None),        # 1 字标题；full = 标题+正文 ≥ 20，body < 20
                                 ("只有正文没有标题的一篇，正文写得足够长足够长足够长。", "none", None),
                                 ("", "none", None), ("正文", "column", "标题列的标题")]:
        sp = S.build_spans(raw, mode=mode, title_col=title_col)
        _, skipped = S.askable(b, sp)
        want = {}
        for q in tv.llm_questions(tv.load_bank(FQ)):
            sc = q["scope"]
            if sc == "title" and sp["title"] is None:
                want[q["id"]] = "no_title"
            elif tv.visible_len(sp[sc] or "") < (tv.MIN_BODY_CHARS if sc in ("body", "full") else 2):
                want[q["id"]] = "text_too_short"
        assert skipped == want, raw


def test_all_skipped_means_zero_calls_and_null_rows():
    b = B.load_bank(FQ)
    jev = FixedJev({})
    r = C.judge_note(jev, b, "n-empty", "#话题 #标签", title_extraction="none")
    assert jev.calls == 0 and r.calls == 0 and len(r.items) == 20
    assert all(it["answer"] is None and it["invalid_reason"] in ("no_title", "text_too_short") for it in r.items.values())


def test_missing_out_of_vocab_and_evidence_contract():
    b = B.load_bank(FQ)
    raw = "标题：戒烟第几天最难熬？\n正文：戒烟第15天，记录一下。昨天在药店看到咀嚼胶就买了一盒。嚼了几口辣嗓子，不太习惯。"
    jev = FixedJev({"title_is_question": {"type": "noul", "noul": 0.9},                  # 是 → 要证据，证据调用给「1」
                    "has_specific_time": {"type": "noul"},                               # 没有数值 → out_of_vocab
                    "opening_type": {"type": "choice", "choice": "不存在的选项", "probabilities": {"不存在的选项": 0.9}},
                    "product_role": {"type": "choice", "choice": "解决方案", "probabilities": {"解决方案": 0.7, "主角": 0.3}}})
    r = C.judge_note(jev, b, "n1", raw)
    assert r.items["has_specific_place"]["invalid_reason"] == "missing" and r.items["has_specific_place"]["p"] is None
    assert r.items["has_specific_time"]["invalid_reason"] == "out_of_vocab"
    assert r.items["opening_type"]["invalid_reason"] == "out_of_vocab" and r.items["opening_type"]["answer"] is None
    assert r.items["title_is_question"]["evidence"] and r.items["title_is_question"].get("invalid_reason") is None
    assert r.items["product_role"]["p"] == 0.7                                            # prob = 所选答案的概率
    rows = {x["question_id"]: x for x in C.ledger_rows(r, b)}
    assert rows["has_specific_place"]["answer"] is None and rows["has_specific_place"]["prob"] is None
    # 不做证据调用：答「是」的行没有证据 → evidence_not_found（TV 守卫 3），账本 answer 为 NULL
    r2 = C.judge_note(FixedJev({"title_is_question": {"type": "noul", "noul": 0.9}}), b, "n2", raw, with_evidence=False)
    assert r2.items["title_is_question"]["invalid_reason"] == "evidence_not_found"
    assert {x["question_id"]: x for x in C.ledger_rows(r2, b)}["title_is_question"]["answer"] is None


def test_tv_choice_evidence_rule_is_parsed_generically():
    assert B._tv_choice_evidence("不需要（这一段就是第一句本身）。") == "never"
    assert B._tv_choice_evidence("选「未出现」以外的选项时，抄产品出现的那一处。") == "on_choice_except:未出现"
    assert B._tv_choice_evidence("抄那一处。") == "on_choice_except:"                    # 新 choice 题默认都要证据，同 TV
    q = B.Question("x", "choice", "?", {"a": "1", "b": "2"}, evidence=B._tv_choice_evidence("抄那一处。"))
    assert q.needs_evidence("a") and q.needs_evidence("b")
    for tvq in S._tv.llm_questions(S._tv.load_bank(FQ)):                                  # 现有 20 题逐题与 TV 的 evidence_required 等价
        jq = B.load_bank(FQ).by_id()[tvq["id"]]
        vals = S._tv.closed_set(tvq)
        for v in vals:
            raw = (v == "是") if tvq["type"] == "bool" else v
            assert jq.needs_evidence(raw) == S._tv.evidence_required(tvq, v), (tvq["id"], v)


def test_apply_evidence_rejects_non_positive_index():
    items = {"q": {"answer": "是", "raw": True}}
    C.apply_evidence(items, {"answers": {"q": {"choice": "0", "probabilities": {"0": 0.9}}}}, {"q": ["第一句", "最后一句"]})
    assert items["q"]["evidence"] is None and items["q"]["invalid_reason"] == "evidence_not_found"


def test_split_sentences_cap_follows_jev_option_limit():
    text = "。".join(f"第{i}句话写得长一点" for i in range(400)) + "。"
    sents = S.split_sentences(text)
    assert len(sents) == S.EVIDENCE_SENT_CAP == 254


def test_jev_retries_529_and_mock_is_order_independent():
    assert 529 in RETRY_STATUSES
    q = {"a": {"type": "noul", "criteria": {"true": "t", "false": "f"}}, "b": {"type": "choice", "criteria": {"x": "1", "y": "2"}}}
    r1 = mock_response({"state": {"s": 1}, "questions": q})
    r2 = mock_response({"state": {"s": 1}, "questions": dict(reversed(list(q.items())))})
    assert r1["answers"] == r2["answers"]


def test_postgrest_upsert_refreshes_extracted_at(monkeypatch):
    import urllib.request
    sent = []

    class Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: sent.append(json.loads(req.data)) or Resp())
    n = C.postgrest_upsert([{"subject_id": "x", "extractor": "jev:1.13.0"}], "http://h", "k")
    assert n == 1 and sent[0][0]["extracted_at"]


# ── 闸二门禁 · 暗题 ──

def test_gate2_unvalidated_fq_targets_are_recorded_not_planned():
    from judge import loop as L
    fq = B.load_bank(FQ, name="feature_questions_v0_1")
    dj = L.judge_draft(JevClient(mock=True), {"title": "健身房教练劝我先戒烟", "body": BODY}, fq=fq)
    now = dj.profile["opening_type"]
    want = next(v for v in fq.by_id()["opening_type"].criteria if v != now)
    recorded: list = []
    plan = L.repair_plan(dj, {fq.name: fq}, target={"opening_type": want}, recorded=recorded)
    assert plan == [] and recorded and recorded[0]["qid"] == "opening_type" and "闸二" in recorded[0]["why"]
    plan2 = L.repair_plan(dj, {fq.name: fq}, target={"opening_type": want}, validated={"opening_type"})
    assert plan2[0]["qid"] == "opening_type" and fq.by_id()["opening_type"].criteria[want] in plan2[0]["instruction"]
    # 换名加载的 fq（按格式认，不按写死的名字）守卫照样生效
    fq2 = B.load_bank(FQ, name="fq_renamed")
    dj2 = L.judge_draft(JevClient(mock=True), {"title": "健身房教练劝我先戒烟", "body": BODY}, fq=fq2)
    assert L.repair_plan(dj2, {fq2.name: fq2}, target={"opening_type": want}) == []
    assert "此题未过闸二" not in L.repair_prompt({"title": "t", "body": BODY}, plan)


def test_hidden_rotation_is_quarterly_and_a_third():
    from judge import hidden as Hd
    ids = B.load_bank(HF2).ids()
    q3, q4 = Hd.hidden_ids("human_feel_para_v0.2", ids, date(2026, 9, 1)), Hd.hidden_ids("human_feel_para_v0.2", ids, date(2026, 11, 1))
    assert len(q3) == len(q4) == 2 and q3 <= set(ids)
    assert Hd.hidden_ids("human_feel_para_v0.2", ids, date(2026, 8, 1)) == q3                # 同一季度不变
    assert Hd.hidden_ids("platform_health_v0.1", B.load_bank(PH).ids()) == set()             # 只有人感题库有暗题
    assert any(Hd.hidden_ids("human_feel_para_v0.2", ids, date(y, m, 1)) != q3 for y in (2027, 2028) for m in (2, 5, 8, 11))   # 会轮换


def test_hidden_questions_never_reach_writer_facing_outputs(monkeypatch, policy_cfg):
    from judge import hidden as Hd, loop as L
    monkeypatch.setattr(Hd, "hidden_ids", lambda name, qids, when=None: {"para_register", "para_friction"} & set(qids) if name.startswith("human_feel") else set())
    fq, ph, hf = B.load_bank(FQ, name="feature_questions_v0_1"), B.load_bank(PH, name="platform_health_v0.1"), B.load_bank(HF2, name="human_feel_para_v0.2")
    dj = L.judge_draft(JevClient(mock=True), {"title": "t", "body": BODY}, fq=fq, platform=ph, human=hf, judge_paras="always")
    for p in dj.para_items:                                      # 让暗题在每段都「不过」
        p["items"]["para_register"] = {"answer": "文案腔", "p": 0.9}
    hidden = Hd.all_hidden({hf.name: hf})
    assert hidden == {"para_register", "para_friction"}
    view = L.redact(dj, hidden)
    blob = json.dumps(view, ensure_ascii=False)
    assert "para_register" not in blob and "para_friction" not in blob and "has_friction" not in view["para_stats"] and "register" not in view["para_stats"]
    plan = L.repair_plan(dj, {fq.name: fq, ph.name: ph, hf.name: hf}, hidden=hidden)
    hid = [p for p in plan if p["qid"] == "hidden"]
    assert hid and all("不公开" in p["instruction"] for p in hid)
    assert not any(k in json.dumps(plan, ensure_ascii=False) for k in ("para_register", "文案", hf.by_id()["para_register"].instructions))
    # HTTP 与 MCP 都不回暗题
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k"); monkeypatch.setenv("JUDGE_PROJECT", "TUGE")
    from fastapi.testclient import TestClient
    from judge import api as A, mcp_server as M
    monkeypatch.setattr(A, "all_hidden", lambda banks: hidden); monkeypatch.setattr(M, "all_hidden", lambda banks: hidden)
    monkeypatch.setattr(M, "hidden_ids", lambda name, qids, when=None: hidden & set(qids))
    c = TestClient(A.app)
    d = c.post("/judge_draft", json={"title": "t", "body": BODY, "project": "TUGE", "judge_paras": "always", "return_rows": False},
               headers={"X-Judge-Key": "k"}).json()
    assert "para_register" not in json.dumps(d, ensure_ascii=False) and "para_friction" not in json.dumps(d, ensure_ascii=False)
    hf_listed = next(x for x in M.list_banks() if x["name"] == "human_feel_para_v0.2")
    assert hf_listed["hidden"] == 2 and not ({"para_register", "para_friction"} & set(hf_listed["questions"]))
    assert "para_register" not in json.dumps(M.judge_draft("t", BODY, judge_paras="always"), ensure_ascii=False)


# ── 数据出境 ──

def test_data_policy_http(monkeypatch, policy_cfg):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge import api as A
    c = TestClient(A.app); H = {"X-Judge-Key": "k"}
    called = []
    real = A._client
    monkeypatch.setattr(A, "_client", lambda: called.append(1) or real())
    brief = {"hard_rules": [{"id": "must_ask", "ask": "有没有向读者提问？", "want": True}]}
    body = {"title": "t", "body": BODY, "subject_id": "v1", "brief": brief}
    assert c.post("/judge_draft", json=body, headers=H).status_code == 422                          # 未发布稿不带 project
    r = c.post("/judge_draft", json=dict(body, project="OKMAN"), headers=H)
    assert r.status_code == 403 and r.json()["detail"].startswith("policy:")                         # 处方药项目：一次 Jev 都不调
    assert c.post("/judge_draft", json=dict(body, project="ANY", category="处方药"), headers=H).status_code == 403
    assert called == []
    d = c.post("/judge_draft", json=dict(body, project="NRT"), headers=H).json()                    # 没放行项目层：整层去掉并回显
    assert d["policy"]["dropped_banks"] == ["project"] and d["policy"]["dropped_hard_rules"] == ["project:must_ask"]
    assert "project" not in d["detail"] and not any(r["question_id"] == "must_ask" for r in d["ledger_rows"])
    d2 = c.post("/judge_draft", json=dict(body, project="TUGE"), headers=H).json()                  # 放行了：照判
    assert "project" in d2["detail"] and d2["policy"]["dropped_banks"] == []
    # 调用方显式指向被去掉那一层的 hard_rules：跟着去掉，不 422
    d3 = c.post("/judge_draft", json=dict(body, project="NRT", hard_rules={"project:must_ask": True}), headers=H)
    assert d3.status_code == 200 and "project:must_ask" in d3.json()["policy"]["dropped_hard_rules"]
    # /judge：公开内容不受限；ssll_sample 要 project，处方药 403
    note = {"subject_type": "note", "subject_id": "n1", "raw_content": "标题：测试？\n正文：" + BODY}
    assert c.post("/judge", json={"bank": "feature_questions_v0_1", "subjects": [note]}, headers=H).status_code == 200
    samp = dict(note, subject_type="ssll_sample", subject_id="run1:c1:g0:1")
    assert c.post("/judge", json={"bank": "feature_questions_v0_1", "subjects": [samp]}, headers=H).status_code == 422
    assert c.post("/judge", json={"bank": "feature_questions_v0_1", "subjects": [samp], "project": "OKMAN"}, headers=H).status_code == 403
    ok = c.post("/judge", json={"bank": "feature_questions_v0_1", "subjects": [samp], "project": "NRT"}, headers=H).json()
    assert ok["policy"]["published"] is False and ok["results"][0]["subject_type"] == "ssll_sample"
    # 预埋评论：subject_type 是 comment，但帖子没发 → published=false 收紧成未发布；published=true 放不宽 ssll_sample
    seed = {"subject_type": "comment", "subject_id": "seed1", "state": {"评论原文": "在哪里"}}
    assert c.post("/judge", json={"bank": "comment_reader_v0.3", "subjects": [seed], "published": False, "project": "OKMAN"}, headers=H).status_code == 403
    assert c.post("/judge", json={"bank": "comment_reader_v0.3", "subjects": [seed], "project": "OKMAN"}, headers=H).status_code == 200
    assert c.post("/judge", json={"bank": "feature_questions_v0_1", "subjects": [samp], "published": True}, headers=H).status_code == 422


def test_data_policy_mcp(monkeypatch, policy_cfg):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.delenv("JUDGE_PROJECT", raising=False)
    from judge import mcp_server as M
    from judge.policy import PolicyBlocked, PolicyInputError
    with pytest.raises(PolicyInputError):
        M.judge_draft("t", BODY)
    with pytest.raises(PolicyBlocked):
        M.judge_draft("t", BODY, project="OKMAN")
    with pytest.raises(PolicyBlocked):
        M.judge_thread("t", BODY, [{"text": "在哪里"}], project="OKMAN")
    brief = {"hard_rules": [{"id": "must_ask", "ask": "有没有向读者提问？", "want": True}]}
    d = M.judge_draft("t", BODY, project="TUGE", brief=brief)                                  # 写手侧也能送 brief、硬约束接到判定
    assert "project" in d["detail"] and d["policy"]["project_layer"]
    plan_ids = {p["qid"] for p in M.repair_plan_for("t", BODY, project="TUGE", brief=dict(brief, hard_rules=[dict(brief["hard_rules"][0], want=(d["profile"]["must_ask"] != "是"))]))}
    assert "must_ask" in plan_ids                                                               # repair_plan_for 也带项目层（以前连 project 都没传）


# ── 回路：太短、没判出来的硬约束、Echo 修补、生成端适配 ──

def test_draft_too_short_and_unjudged_hard_rules():
    from judge import loop as L
    ph = B.load_bank(PH, name="platform_health_v0.1")
    jev = FixedJev({})
    dj = L.judge_draft(jev, {"title": "t", "body": "太短了"}, platform=ph, hard_rules={("platform_health_v0.1", "efficacy_claim"): "否"})
    assert dj.invalid_reason == "text_too_short" and not dj.passed() and jev.calls == 0
    dj2 = L.judge_draft(FixedJev({}), {"title": "t", "body": BODY}, platform=ph, hard_rules={("platform_health_v0.1", "efficacy_claim"): "否"})
    assert dj2.unjudged == [("platform_health_v0.1", "efficacy_claim", "missing")] and not dj2.passed() and not dj2.hard_fails


def test_echo_generator_repairs_by_deleting_evidence_sentence():
    from judge import loop as L
    plan = [{"instruction": "有一条合规硬约束没过（0.9）。依据句：效果是真的绝，立马就压住了。"}]
    draft = {"title": "戒烟", "body": "第一天很难熬。效果是真的绝，立马就压住了。第二天好多了。"}
    out = L.repair(L.EchoGenerator(), draft, plan)
    assert out == {"title": "戒烟", "body": "第一天很难熬。第二天好多了。"}


def test_anthropic_compat_generator_base_url_and_key(monkeypatch):
    from judge import loop as L
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False); monkeypatch.setenv("MOONSHOT_API_KEY", "ms")
    g = L.AnthropicCompatGenerator("kimi", base_url="https://relay.example.com/v1/")
    assert g.base_url == "https://relay.example.com" and g.api_key == "ms"


def test_mcp_client_is_cached(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1")
    from judge import mcp_server as M
    monkeypatch.setattr(M, "_CLIENT", None)
    assert M._client() is M._client()


# ── 评论：摩擦换位、配置问题不白烧、漏答对称、needs_review 不算过 ──

def _cj(text, **profile):
    from judge import comments as CM
    cj = CM.CommentJudgement(text=text)
    cj.items = {k: {"answer": v, "p": 0.9} for k, v in profile.items()}
    return cj


def test_thread_repair_plan_config_problems_and_friction():
    from judge import comments as CM
    tj = CM.ThreadJudgement(hard_fails=[("named_count", "2", "≤1", None)])
    slots = [CM.Slot("a", ["补充经验"], may_name_brand=True), CM.Slot("b", ["补充经验"], may_name_brand=True)]
    judged = [_cj("x", names_brand="是"), _cj("y", names_brand="是")]
    plan = CM.thread_repair_plan(tj, slots, judged)
    assert [p["slot"] for p in plan] == [None] and "配置" in plan[0]["why"]                  # 换谁都修不了：不换位
    tj2 = CM.ThreadJudgement(hard_fails=[("has_friction", "否", "是", 0.8)])
    only_exp = [CM.Slot("e1", ["补充经验"]), CM.Slot("e2", ["补充经验"])]
    assert [p["slot"] for p in CM.thread_repair_plan(tj2, only_exp, [_cj("x"), _cj("y")])] == [None]
    with_reply = only_exp + [CM.Slot("r", ["回复答疑"], role="贴主", reply_to="e1")]
    assert [p["slot"] for p in CM.thread_repair_plan(tj2, with_reply, [_cj("x"), _cj("y"), _cj("z")])] == ["r"]
    # brief 硬放的背书位：同一句式时不换（重出还是背书体），标配置问题
    tj3 = CM.ThreadJudgement(hard_fails=[("same_template", "是", "否", 0.9)])
    endorse_slot = [CM.Slot("en", ["亲历背书"])]
    cj = _cj("x", speech_act="亲历背书"); cj.flags = ["背书体"]
    assert [p["slot"] for p in CM.thread_repair_plan(tj3, endorse_slot, [cj])] == [None]


def test_judge_comment_missing_answers_go_to_review_not_hard_fail():
    from judge import comments as CM
    reader = B.load_bank(ROOT / "banks" / "comment_reader_v0.4.yaml")
    post = {"title": "t", "body": "戒烟第三天，嘴里没味，靠嗑瓜子撑着。", "kind": "product"}
    jev = FixedJev({"speech_act": {"type": "choice", "choice": "提问", "probabilities": {"提问": 0.9, "质疑": 0.1}}})
    cj = CM.judge_comment(jev, post, "在哪买", CM.default_slots()[2], reader, fill=CM.fill_for(post))
    assert "漏答" in cj.flags and cj.needs_review
    assert not any(f[1] is None for f in cj.hard_fails)                                        # 不会下发「现在判『None』」
    assert {f[0] for f in cj.hard_fails} == {"speech_act"}                                     # 只有判出来的题参与硬伤


def test_comment_repair_plan_strips_prefix_not_charset():
    from judge import comments as CM
    reader = B.load_bank(ROOT / "banks" / "comment_reader_v0.4.yaml")
    post = {"title": "t", "body": "戒烟第三天，嘴里没味，靠嗑瓜子撑着。", "kind": "product"}
    cj = CM.CommentJudgement(text="x", hard_fails=[("detail_level", "无", "至少模糊", 0.8)])
    plan = CM.comment_repair_plan(cj, {"r": reader}, CM.fill_for(post))
    assert plan[0]["definition"] == B.fill_text(reader.by_id()["detail_level"].criteria["模糊"], CM.fill_for(post))


def test_new_bank_versions_have_exits():
    hf = B.load_bank(HF2).by_id()
    assert hf["para_function"].unclear_labels == ["说不清"] and "说不清" in hf["para_function"].criteria
    th = B.load_bank(ROOT / "banks" / "comment_thread_v0.4.yaml").by_id()
    assert th["praise_share"].unclear_labels == ["评论太少"]
    v1 = B.load_bank(ROOT / "banks" / "human_feel_para_v0.1.yaml")
    assert [q.id for q in v1.questions] == list(hf)                                            # 只加出口，不加题


# ── Mode A 卫生 ──

def test_mode_a_no_performance_words_in_banks_or_states():
    kws = S._tv.PERFORMANCE_KEYWORDS
    for name, path in B.discover(ROOT / "banks").items():
        b = B.load_bank(path, name=name)
        for q in b.questions:
            for text in [q.instructions] + [str(v) for v in q.criteria.values()]:
                assert not any(k in text for k in kws), (name, q.id, text)
    from judge import comments as CM, external as E
    import importlib.util
    spec = importlib.util.spec_from_file_location("bf", ROOT / "scripts" / "backfill_comments.py"); bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
    post = {"title": "t", "body": "b", "kind": "product"}
    states = [S.state_from_spans(S.build_spans("标题：t\n正文：b", mode="markers")), CM.comment_state(post, "c", reply_to_text="r", role="贴主"),
              CM.thread_state(post, [{"text": "c"}]), E.triage_state({"title": "t", "body": "d", "author": "a", "keyword": "戒烟"}, "健康"),
              bf.state_ops({"comment_text": "c"}), bf.state_reader({"comment_text": "c"})]
    for fx in ("thread_cases.json", "comment_cases.json"):
        d = json.loads((ROOT / "fixtures" / fx).read_text(encoding="utf-8"))
        states.append(d.get("shared") or {})
    for st in states:
        blob = json.dumps(st, ensure_ascii=False)
        assert not any(k in blob for k in kws), blob[:200]


# ── 外部语料 · 影子跑 · 金标准 ──

def test_find_notes_note_wrapper_and_non_dicts():
    from judge import external as E
    raw = {"data": {"items": [{"model_type": "ad"}, "garbage", {"note": {"id": "n1", "title": "戒烟第三天", "desc": "嘴里没味"}}]}}
    notes = E._find_notes(raw)
    assert len(notes) == 1 and E.normalize_note(notes[0], "戒烟", "健康", "general")["note_id"] == "n1"


def test_fq_shadow_mappings_and_latest_extractor(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("fqs", ROOT / "scripts" / "fq_shadow.py"); fqs = importlib.util.module_from_spec(spec); spec.loader.exec_module(fqs)
    (tmp_path / "A_phase1.yaml").write_text("project_id: A_phase1\ntitle_extraction: column\n", encoding="utf-8")
    (tmp_path / "B_phase1.yaml").write_text("project_id: B_phase1\n", encoding="utf-8")
    (tmp_path / "_template.yaml").write_text("title_extraction: markers\n", encoding="utf-8")
    assert fqs.load_mappings(str(tmp_path)) == {"A_phase1": "column", "B_phase1": "none"}
    rows = [{"subject_id": "s", "question_id": "q", "answer": "是", "evidence": "e", "invalid_reason": None, "extractor": "llm:old", "extracted_at": "2026-01-01"},
            {"subject_id": "s", "question_id": "q", "answer": "否", "evidence": None, "invalid_reason": None, "extractor": "llm:new", "extracted_at": "2026-09-01"}]
    assert fqs.latest_per_cell(rows)["s"]["q"]["extractor"] == "llm:new"
    assert fqs.latest_per_cell(list(reversed(rows)))["s"]["q"]["answer"] == "否"


def test_known_external_ids_without_env(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("kei", ROOT / "scripts" / "known_external_ids.py"); kei = importlib.util.module_from_spec(spec); spec.loader.exec_module(kei)
    out = tmp_path / "k.txt"
    assert kei.main([str(out)]) == 0 and out.read_text(encoding="utf-8") == ""


def test_run_gold_report_names_gold_version(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rg", ROOT / "scripts" / "run_gold.py"); rg = importlib.util.module_from_spec(spec); spec.loader.exec_module(rg)
    out = tmp_path / "r.md"
    rg.main([str(ROOT / "banks" / "comment_reader_v0.4.yaml"), str(ROOT / "fixtures" / "comment_cases.json"),
             "--gold", str(ROOT / "banks" / "gold" / "comment_reader_gold_v0.2_proposed.yaml"), "--mock", "--out", str(out)])
    head = out.read_text(encoding="utf-8").splitlines()[:3]
    assert "金标准 cg-v0.2-proposed" in head[0] and "未经勘误" in "\n".join(head)
