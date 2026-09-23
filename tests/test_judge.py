# -*- coding: utf-8 -*-
"""全部用 mock，不联网。跑：python3 -m pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import core as C  # noqa: E402
from judge import spans as S  # noqa: E402
from judge.jev_client import JevClient, mock_response  # noqa: E402

FQ = ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml"
READER = ROOT / "banks" / "comment_reader_v0.3.yaml"
OPS = ROOT / "banks" / "comment_ops_v0.1.yaml"
THREAD = ROOT / "banks" / "comment_thread_v0.2.yaml"


# ── 题库 ──

def test_vendor_checksums_match():
    sums = (ROOT / "banks" / "vendor" / "SHA256SUMS").read_text(encoding="utf-8").split()
    want = {Path(p).name: h for h, p in zip(sums[0::2], sums[1::2])}
    import hashlib
    assert hashlib.sha256(FQ.read_bytes()).hexdigest() == want["feature_questions_v0_1.yaml"]
    assert hashlib.sha256((ROOT / "banks" / "vendor" / "tv_feature_bank.py").read_bytes()).hexdigest() == want["feature_bank.py"]


@pytest.mark.parametrize("path,fmt,n", [(FQ, "tv", 20), (READER, "jev", 7), (OPS, "jev", 2), (THREAD, "jev", 4)])
def test_load_and_check(path, fmt, n):
    b = B.load_bank(path)
    assert b.fmt == fmt and len(b.questions) == n and B.check_bank(b) == []
    assert b.sha256 and b.version


def test_tv_bank_questions_carry_scope_and_evidence():
    b = B.load_bank(FQ).by_id()
    assert b["title_is_question"].scope == "title" and b["title_is_question"].jtype == "noul"
    assert b["opening_type"].jtype == "choice" and b["opening_type"].evidence == "never"
    assert b["product_role"].needs_evidence("主角") and not b["product_role"].needs_evidence("未出现")
    assert b["has_specific_time"].needs_evidence(True) and not b["has_specific_time"].needs_evidence(False)
    q = B.build_questions(B.load_bank(FQ), ["title_is_question", "opening_type"])
    assert q["title_is_question"]["type"] == "noul" and "只看【标题】" in q["title_is_question"]["instructions"]
    assert set(q["opening_type"]["criteria"]) == {"具体事件", "身份自述", "观点断言", "提问", "数据事实", "感叹情绪", "其他"}


def test_ops_bank_labels_match_tv_check_constraint():
    labels = list(B.load_bank(OPS).by_id()["comment_intent"].criteria)
    assert labels == ["补充信息", "反驳质疑", "蓝词植入", "共鸣扩散", "引导私信", "其他"]


# ── 解读与歧义 ──

def test_interpret_noul_and_choice_thresholds():
    b = B.load_bank(READER)
    resp = {"answers": {"names_brand": {"type": "noul", "noul": 0.97},
                        "arranged": {"type": "noul", "noul": 0.43},
                        "speech_act": {"type": "choice", "choice": "亲历背书", "confidence": 0.9,
                                       "probabilities": {"亲历背书": 0.98, "旁观推荐": 0.02}},
                        "register": {"type": "choice", "choice": "文案腔", "confidence": 0.4,
                                     "probabilities": {"文案腔": 0.49, "认真分享": 0.41, "随手口语": 0.1}}}}
    it = B.interpret(b, resp)
    assert it["names_brand"]["answer"] == "是" and it["names_brand"]["p"] == 0.97 and not it["names_brand"]["ambiguous"]
    assert it["arranged"]["answer"] == "否" and it["arranged"]["p"] == 0.57 and it["arranged"]["ambiguous"]
    assert it["speech_act"]["answer"] == "亲历背书" and not it["speech_act"]["ambiguous"]
    assert it["register"]["ambiguous"] and "第一名" in it["register"]["why"]


# ── 切片与选句 ──

def test_spans_and_askable():
    b = B.load_bank(FQ)
    sp = S.build_spans("标题：老公戒烟成功但开始疯狂吃零食了\n正文： 老公戒烟到今天正好40天了，烟是真的没抽。\n但是他现在变成了零食狂人。\n有没有同款老公的？求个解决办法\n#戒烟成功", mode="markers")
    assert sp["title"] == "老公戒烟成功但开始疯狂吃零食了"
    qids, skipped = S.askable(b, sp)
    assert len(qids) == 20 and skipped == {}
    sp2 = S.build_spans("【正文】\n情窦初开的时候父母不同意，情窦再开的时候老婆不同意。\n好遗憾。", mode="markers")
    qids2, skipped2 = S.askable(b, sp2)
    assert skipped2 == {"title_is_question": "no_title"} and len(qids2) == 19
    st = S.state_from_spans(sp2)
    assert st["标题"] == "（这篇没有标题）" and "说明" in st


def test_evidence_call_and_apply():
    b = B.load_bank(FQ)
    sp = S.build_spans("标题：戒烟第几天最难熬？\n正文：戒烟第15天，记录一下。昨天在药店看到咀嚼胶就买了一盒。嚼了几口辣嗓子。", mode="markers")
    items = {"title_is_question": {"answer": "是", "raw": True}, "has_specific_time": {"answer": "是", "raw": True},
             "has_specific_place": {"answer": "否", "raw": False}, "product_role": {"answer": "解决方案", "raw": "解决方案"},
             "opening_type": {"answer": "具体事件", "raw": "具体事件"}}
    body, sent_map = C.build_evidence_call(b, sp, items)
    assert set(body["questions"]) == {"title_is_question", "has_specific_time", "product_role"}
    assert "没有" in body["questions"]["has_specific_time"]["criteria"]
    resp = {"answers": {"title_is_question": {"choice": "1", "probabilities": {"1": 0.9}},
                        "has_specific_time": {"choice": "1", "probabilities": {"1": 0.8}},
                        "product_role": {"choice": "没有", "probabilities": {"没有": 0.7}}}}
    C.apply_evidence(items, resp, sent_map)
    assert items["title_is_question"]["evidence"] == "戒烟第几天最难熬？"
    assert items["has_specific_time"]["evidence"].startswith("戒烟第15天")
    assert items["product_role"]["invalid_reason"] == "evidence_not_found" and items["product_role"]["evidence"] is None


# ── 端到端（mock）与账本 ──

def test_judge_note_mock_and_ledger_sql():
    b = B.load_bank(FQ)
    client = JevClient(mock=True)
    r = C.judge_note(client, b, "NUC_phase1_recTEST", "标题：普通朋友住院送什么？\n正文： 一个普通朋友突发阑尾炎割了，预算200左右。水果篮太沉，鲜花不实用。大家平时都送什么啊？", with_evidence=True)
    assert r.subject_type == "note" and len(r.items) == 20 and r.calls in (1, 2)
    rows = C.ledger_rows(r, b, run_tag="shadow-test")
    assert len(rows) == 20 and {x["run_tag"] for x in rows} == {"shadow-test"} and {x["extractor"] for x in rows} == {"jev:1.13.0"}
    for x in rows:
        assert set(x) == set(C.LEDGER_COLUMNS) and x["bank_version"] == "fq-v0.1" and x["bank_sha256"] == b.sha256
        if x["invalid_reason"]:
            assert x["answer"] is None and x["evidence"] is None
        elif x["answer"] is not None:
            assert 0 <= x["prob"] <= 1
    sql = C.rows_to_sql(rows)
    assert sql.startswith("INSERT INTO truth_vault.note_feature_answers") and "ON CONFLICT (subject_type, subject_id, question_id, question_version, extractor, run_tag)" in sql
    assert "'NUC_phase1_recTEST'" in sql and sql.count("VALUES") == 1


def test_judge_state_mock_and_determinism():
    b = B.load_bank(READER)
    client = JevClient(mock=True)
    st = {"帖子标题": "x", "评论原文": "在哪里"}
    r1 = C.judge_state(client, b, "c1", st); r2 = C.judge_state(client, b, "c1", st)
    assert r1.items == r2.items and set(r1.items) == set(b.ids())
    rows = C.ledger_rows(r1, b, run_tag="primary")
    assert {x["subject_type"] for x in rows} == {"comment"}


def test_mock_response_shape():
    body = {"state": {"a": 1}, "questions": {"q1": {"type": "noul", "criteria": {"true": "t", "false": "f"}},
                                            "q2": {"type": "choice", "criteria": {"x": "1", "y": "2"}}}}
    resp = mock_response(body)
    assert 0 <= resp["answers"]["q1"]["noul"] <= 1 and resp["answers"]["q2"]["choice"] in ("x", "y")
    assert abs(sum(resp["answers"]["q2"]["probabilities"].values()) - 1) < 1e-9


# ── API ──

def test_api_health_banks_and_judge_mock(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge.api import app
    c = TestClient(app)
    h = c.get("/health").json()
    assert h["ok"] and "feature_questions_v0_1" in h["banks"] and "comment_reader_v0.3" in h["banks"]
    assert c.get("/banks").status_code == 401
    bl = c.get("/banks", headers={"X-Judge-Key": "k"}).json()
    assert all(x["problems"] == [] for x in bl)
    body = {"bank": "feature_questions_v0_1", "run_tag": "shadow-t", "subjects": [
        {"subject_type": "note", "subject_id": "n1", "raw_content": "标题：测试标题？\n正文：昨天在药店买了一盒东西，嚼了几口辣嗓子，有点想戒了。大家怎么看？"}]}
    r = c.post("/judge", json=body, headers={"X-Judge-Key": "k"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rows"] == 20 and d["results"][0]["subject_id"] == "n1" and d["written"] is None
    body2 = {"bank": "comment_reader_v0.3", "subjects": [{"subject_type": "comment", "subject_id": "c1", "state": {"评论原文": "在哪里"}}]}
    r2 = c.post("/judge", json=body2, headers={"X-Judge-Key": "k"})
    assert r2.status_code == 200 and r2.json()["rows"] == 7
    assert c.post("/judge", json={"bank": "nope", "subjects": [{"subject_id": "x", "state": {}}]}, headers={"X-Judge-Key": "k"}).status_code == 404


# ── 生产回路（mock + Echo 生成端）──

def test_loop_compile_judge_repair_produce():
    from judge import loop as L
    client = JevClient(mock=True)
    fq = B.load_bank(FQ, name="feature_questions_v0_1")
    ph = B.load_bank(ROOT / "banks" / "platform_health_v0.1.yaml", name="platform_health_v0.1")
    hf = B.load_bank(ROOT / "banks" / "human_feel_para_v0.1.yaml", name="human_feel_para_v0.1")
    proj = L.compile_project_bank({"intents": [{"label": "烟瘾场景", "means": "想抽烟的时刻"}],
                                   "hard_rules": [{"id": "no_price", "ask": "有没有说便宜？", "yes": "说了", "no": "没说", "want": False}],
                                   "angle": {"label": "教练劝戒烟", "means": "健身房"}})
    assert proj.ids() == ["intent_of_para", "no_price", "on_angle"] and B.check_bank(proj) == []
    draft = {"title": "健身房教练劝我先戒烟", "body": "上周办了张健身卡。\n教练问我是不是抽烟。\n办卡花了三千多，挺亏的。\n你们是先戒烟还是边练边戒？"}
    hard = {("platform_health_v0.1", "efficacy_claim"): "否", ("project", "no_price"): "否"}
    dj = L.judge_draft(client, draft, fq=fq, platform=ph, project=proj, human=hf, judge_paras="always", hard_rules=hard)
    assert set(dj.detail) == {"feature_questions_v0_1", "platform_health_v0.1", "project"}
    assert len(dj.para_items) == 4 and dj.para_stats["n"] == 4 and "intents" in dj.para_stats
    assert all("intent_of_para" in p["items"] for p in dj.para_items)
    plan = L.repair_plan(dj, {"feature_questions_v0_1": fq, "platform_health_v0.1": ph, "project": proj, "human_feel_para_v0.1": hf})
    assert isinstance(plan, list) and all("instruction" in p for p in plan)
    assert L.parse_draft("标题：甲\n正文：乙\n丙") == {"title": "甲", "body": "乙\n丙"}
    gen = L.EchoGenerator()
    prompt = "写三版：⟪标题：一\n正文：第一版正文。⟫⟪标题：二\n正文：第二版正文。⟫⟪标题：三\n正文：第三版正文。⟫"
    (best, bj), ranking = L.best_of_k(client, gen, prompt, 3, judge_kwargs=dict(fq=fq, platform=ph, hard_rules=hard), target=None)
    assert len(ranking) == 3 and best["title"] in ("一", "二", "三")
    out = L.produce(client, gen, prompt, k=2, max_repairs=1, fq=fq, platform=ph, project=proj, human=hf, hard_rules=hard)
    assert set(out) >= {"draft", "passed", "profile", "trail", "calls"} and out["trail"][0]["step"] == "best_of_k"


def test_mcp_tools_mock(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1")
    from judge import mcp_server as M
    names = {b["name"] for b in M.list_banks()}
    assert {"feature_questions_v0_1", "comment_reader_v0.3", "platform_health_v0.1", "human_feel_para_v0.1"} <= names
    d = M.judge_draft("测试标题？", "第一段讲事。\n第二段讲感受。\n大家怎么看？")
    assert set(d) >= {"passed", "profile", "hard_fails", "para_stats"}
    plan = M.repair_plan_for("测试标题？", "第一段。\n第二段。")
    assert isinstance(plan, list)
    cs = M.judge_comments("标题", "正文", [{"id": "c1", "text": "在哪里"}, {"id": "c2", "text": "感谢老师帮我拿到结果"}])
    assert len(cs) == 2 and all("flags" in c and "speech_act" in c["items"] for c in cs)


# ── 外部语料：预算、去重、上限（mock 供应商 + mock Jev）──

def test_external_budget_dedupe_caps(tmp_path):
    from judge import external as E
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))
    cfg["categories"] = cfg["categories"][:2]; cfg["pages_per_sort"] = 1; cfg["max_keep_per_category_per_run"] = 5
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    fq = B.load_bank(FQ, name="feature_questions_v0_1")
    jev = JevClient(mock=True)
    # 1) 正常跑：每品类最多 5 篇，搜索页数 = 2 品类 × 4 词 × 2 排序 = 16 之内（到上限就不再搜）
    prov = E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True)
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(cfg, prov, jev, triage, fq, state)
    assert all(s["kept"] <= 5 for s in rep.per_category.values()) and rep.searched <= 16 and rep.stopped_reason == ""
    assert rep.spent_usd == round(rep.calls * 0.01, 4) and len(rep.rows) == rep.judged * (20 + 4)
    kept_ids = {n["note_id"] for n in rep.kept_notes}
    assert all(state["seen"][i]["kept"] for i in kept_ids)
    # 2) 第二次跑：上次见过的全部去重（上次到上限就停搜了，所以还会有新候选），留下的不与上次重复
    prov2 = E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True)
    rep2 = E.run_once(cfg, prov2, jev, triage, fq, state)
    assert rep2.duplicates >= 1 and not (kept_ids & {n["note_id"] for n in rep2.kept_notes})
    # 2b) 把每类上限放到很大再跑一次：这次所有候选都见过 → 全部重复、一篇不留
    cfg_big = dict(cfg); cfg_big["max_keep_per_category_per_run"] = 10**6
    E.run_once(cfg_big, E.TikHubClient(budget=E.Budget(limit_usd=50.0), mock=True), jev, triage, fq, state)
    rep2b = E.run_once(cfg_big, E.TikHubClient(budget=E.Budget(limit_usd=50.0), mock=True), jev, triage, fq, state)
    assert rep2b.duplicates == rep2b.candidates and rep2b.triaged_kept == 0
    # 3) 预算极小：提前停，报告里写原因
    prov3 = E.TikHubClient(budget=E.Budget(limit_usd=0.02), mock=True)
    rep3 = E.run_once(cfg, prov3, jev, triage, fq, {"seen": {}, "monthly": {}})
    assert "预算" in rep3.stopped_reason and rep3.calls <= 2
    # 4) 月上限：已到就整类跳过
    st4 = {"seen": {}, "monthly": {rep.started[:7]: {cfg["categories"][0]["name"]: 999}}}
    rep4 = E.run_once(cfg, E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True), jev, triage, fq, st4)
    assert rep4.per_category[cfg["categories"][0]["name"]].get("note") == "本月上限已到"
    # 5) 外部笔记表 SQL 与账本 SQL
    sql = E.rows_to_sql_external(E.external_note_rows(rep.kept_notes, "ext-test"))
    assert sql.startswith("INSERT INTO truth_vault.external_notes") and "ON CONFLICT (note_id)" in sql
    assert {r["subject_type"] for r in rep.rows} == {"external_note"} and {r["run_tag"] for r in rep.rows} == {"external"}


def test_external_find_notes_and_normalize():
    from judge import external as E
    raw = {"code": 200, "data": {"result": {"items": [{"note_card": {"note_id": "abc", "display_title": "标题", "desc": "正文", "user": {"nickname": "n"},
                                                                     "interact_info": {"liked_count": "1.2万", "comment_count": "3"}}}]}}}
    notes = E._find_notes(raw)
    assert len(notes) == 1
    n = E.normalize_note(notes[0], "kw", "cat", "general")
    assert n["note_id"] == "abc" and n["title"] == "标题" and n["liked"] == 12000 and n["comments"] == 3 and n["author"] == "n"


# ── 评论回路：题库 v0.4 占位符 + 有脚本的假 Jev（按评论文字给答案）──

READER4 = ROOT / "banks" / "comment_reader_v0.4.yaml"
THREAD3 = ROOT / "banks" / "comment_thread_v0.3.yaml"


def test_reader_v04_equals_v03_for_tugeo_case():
    """v0.4 只是把按篇变的词换成占位符；填上途鸽用例的 fill 后，发给 Jev 的题目与 v0.3 逐字相同 → 金标准 cg-v0.2 仍然有效。"""
    cases = json.loads((ROOT / "fixtures" / "comment_cases.json").read_text(encoding="utf-8"))
    b3, b4 = B.load_bank(READER), B.load_bank(READER4)
    assert B.unfilled(b4) == ["echoes_post"] and B.unfilled(b4, cases["fill"]) == []
    assert B.build_questions(b4, fill=cases["fill"]) == B.build_questions(b3)
    t2, t3 = B.load_bank(THREAD), B.load_bank(THREAD3)
    tcases = json.loads((ROOT / "fixtures" / "thread_cases.json").read_text(encoding="utf-8"))
    assert B.build_questions(t3, fill=tcases["fill"]) == B.build_questions(t2)


class ScriptedJev:
    """按评论文字给固定答案的假 Jev：测回路的路由、修改单、best-of-k 和换位，不测模型。"""

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _choice(qid, label, labels, p=0.9):
        rest = (1 - p) / max(1, len(labels) - 1)
        probs = {lab: (p if lab == label else rest) for lab in labels}
        return {"type": "choice", "choice": label, "confidence": p, "probabilities": probs}

    def call(self, body):
        self.calls += 1
        st, qs = body["state"], body["questions"]
        text = st.get("评论原文") or st.get("评论列表") or ""
        ans = {}
        if "评论列表" in st:                                   # 评论区题
            n_praise = text.count("帮我")
            n = max(1, text.count("\n") + 1)
            ans["praise_share"] = self._choice("praise_share", "多数" if n_praise / n > 0.75 else "少数" if n_praise / n < 0.25 else "一半左右", ["少数", "一半左右", "多数"])
            ans["same_template"] = {"type": "noul", "noul": 0.9 if n_praise >= 2 else 0.1}
            ans["has_friction"] = {"type": "noul", "noul": 0.9 if ("[贴主]" in text or "别信" in text) else 0.1}
            ans["thread_arranged"] = {"type": "noul", "noul": 0.8 if n_praise >= 2 else 0.2}
            return {"model": "scripted", "answers": {k: v for k, v in ans.items() if k in qs}, "usage": {}}
        endorse = "帮我" in text
        named = "途鸽" in text or "@" in text
        ask = text.endswith("？") or text.endswith("?") or text in ("在哪里",)
        sa_labels = ["提问", "质疑", "补充经验", "亲历背书", "旁观推荐", "回复答疑", "闲聊表情", "说不清"]
        if "回复对象" in st and not ask:
            sa = "回复答疑"
        elif ask:
            sa = "提问"
        elif endorse:
            sa = "亲历背书"
        elif "别信" in text:
            sa = "质疑"
        else:
            sa = "补充经验"
        ans["speech_act"] = self._choice("speech_act", sa, sa_labels, 0.95)
        ans["names_brand"] = {"type": "noul", "noul": 0.97 if named else 0.05}
        ans["echoes_post"] = {"type": "noul", "noul": 0.9 if ("素材库" in text or "四版" in text) else 0.2}
        ans["detail_level"] = self._choice("detail_level", "具体" if "四版" in text else "无" if (ask or endorse) else "模糊", ["无", "模糊", "具体"])
        ans["register"] = self._choice("register", "文案腔" if endorse else "随手口语" if len(text) < 12 else "认真分享", ["随手口语", "认真分享", "文案腔", "说不清"])
        ans["reader_value"] = self._choice("reader_value", "可行动信息" if named and sa == "回复答疑" else "只有评价" if endorse else "无" if ask else "判断依据",
                                           ["可行动信息", "判断依据", "只有评价", "无"])
        ans["arranged"] = {"type": "noul", "noul": 0.75 if endorse else 0.3}
        ans["comment_intent"] = self._choice("comment_intent", "补充信息" if sa in ("补充经验", "回复答疑") else "共鸣扩散" if endorse else "其他",
                                             ["补充信息", "反驳质疑", "蓝词植入", "共鸣扩散", "引导私信", "其他"])
        ans["is_scripted"] = {"type": "noul", "noul": 0.8 if endorse else 0.2}
        return {"model": "scripted", "answers": {k: v for k, v in ans.items() if k in qs}, "usage": {"input_tokens": 1, "output_tokens": 1}}


POST = {"title": "想问下各位留学生秋招真的不慌吗？", "kind": "agency",
        "body": "我美本文科，之前秋招也是一头雾水。签约前我反复问顾问简历是不是你们写。他们带着我挖素材，三千字的素材库，磨了四版简历。",
        "points": ["美本文科", "反复问简历是不是你们写", "三千字素材库", "四版简历"]}


def test_comment_fill_and_judge_routes():
    from judge import comments as CM
    reader, ops = B.load_bank(READER4), B.load_bank(OPS)
    fill = CM.fill_for(POST)
    assert fill["subject"] == "这家机构" and "四版简历" in fill["post_points"] and fill["post_examples"].startswith("「美本文科」")
    assert CM.fill_for({"title": "t", "body": "戒烟第三天，嘴里没味。", "kind": "product"})["subject"] == "这个产品"
    jev = ScriptedJev()
    # 背书体：只判不算硬伤（slot=None），但打旗 + 进复核
    cj = CM.judge_comment(jev, POST, "文社科真的需要有人帮你挖掘亮点 途鸽老师帮我找到了最适合我的赛道", None, reader, ops=ops, fill=fill)
    assert cj.needs_review and {"背书体", "文案腔", "只有评价", "像安排的", "像脚本"} <= set(cj.flags) and cj.passed()
    # 同一条放到「补充经验」位：五处硬伤，修改单每条带定义
    exp = CM.default_slots()[2]
    assert exp.id == "exp"
    cj2 = CM.judge_comment(jev, POST, "文社科真的需要有人帮你挖掘亮点 途鸽老师帮我找到了最适合我的赛道", exp, reader, ops=ops, fill=fill)
    fails = {f[0] for f in cj2.hard_fails}
    assert fails == {"speech_act", "names_brand", "echoes_post", "register", "reader_value", "detail_level", "comment_intent"}
    plan = CM.comment_repair_plan(cj2, {"reader": reader, "ops": ops}, fill)
    assert len(plan) == len(cj2.hard_fails) and all(p["definition"] for p in plan)
    assert any("不对这家机构下结论" in p["instruction"] for p in plan)          # 占位符已按本篇填好
    assert not any("{" in p["instruction"] for p in plan)
    # 提问位：「在哪里」过
    q1 = CM.default_slots()[0]
    cj3 = CM.judge_comment(jev, POST, "在哪里", q1, reader, ops=ops, fill=fill)
    assert cj3.passed() and cj3.profile()["speech_act"] == "提问"
    # 漏填占位符 → 报错，不会拿默认要点去判
    with pytest.raises(ValueError):
        CM.judge_comment(jev, POST, "在哪里", q1, reader, fill={"subject": "这家机构"})


def test_produce_comments_best_of_k_repair_and_thread_swap():
    from judge import comments as CM
    from judge.loop import EchoGenerator
    reader, ops, thread = B.load_bank(READER4), B.load_bank(OPS), B.load_bank(THREAD3)
    jev = ScriptedJev()
    slots = CM.default_slots(n_questions=1)          # q1 提问 · exp 补充经验 · reply 贴主回复
    # 每一位的候选放在 prompt 的 ⟪…⟫ 里（EchoGenerator 就吐这些）；exp 位第一轮全是背书体，修补时 Echo 吐出修改单 prompt 里的 ⟪…⟫ → 给它一条能过的
    cands = {"q1": ["⟪文科能不能做？⟫", "⟪在哪里⟫"],
             "exp": ["⟪文社科真的需要有人帮你挖掘亮点 途鸽老师帮我找到了最适合我的赛道⟫", "⟪感谢途鸽 帮我度过了最难熬的提前批⟫"],
             "reply": ["⟪@TogoCareer 途鸽求职⟫"]}
    calls_per_slot = {}

    def prompt_for(post, slot):
        calls_per_slot[slot.id] = calls_per_slot.get(slot.id, 0) + 1
        return "\n".join(cands[slot.id])

    class RepairingEcho(EchoGenerator):
        def __call__(self, prompt, n=1):
            if "修改单" in prompt:                      # 修补：给一条按修改单改过的（接住帖子、有情境、不点名）
                return ["我也是文科，当时也反复问了简历是不是他们写，后来四版改下来才踏实"]
            return super().__call__(prompt, n)

    out = CM.produce_comments(jev, RepairingEcho(), POST, slots, prompt_for, reader=reader, thread=thread, ops=ops, k=2, max_repairs=1)
    by = {c["slot"]: c for c in out["comments"]}
    assert by["q1"]["passed"] and by["reply"]["passed"] and by["reply"]["profile"]["names_brand"] == "是"
    assert by["exp"]["passed"] and "四版" in by["exp"]["text"] and by["exp"]["profile"]["speech_act"] == "补充经验"
    steps = [t["step"] for t in out["trail"]]
    assert "best_of_k:q1" in steps and "repair_1:exp" in steps and steps[-1].startswith("thread_")
    assert out["thread"]["items"]["has_friction"]["answer"] == "是"     # 贴主回复算摩擦
    assert out["thread"]["code"] == {"n": 3, "named": 1, "endorse": 0} and out["passed"] and out["calls"] == jev.calls
    # 成组不过 → 换位：三条背书体的组，评论区判同一句式 + 像安排的，thread_repair_plan 指到那几位
    bad = [CM.judge_comment(jev, POST, t, None, reader, fill=CM.fill_for(POST)) for t in
           ["途鸽老师帮我本土化了", "感谢途鸽 帮我度过了提前批", "在哪里"]]
    tj = CM.judge_thread(jev, POST, [{"text": cj.text} for cj in bad], thread, fill=CM.fill_for(POST), judged=bad, max_named=1)
    assert {f[0] for f in tj.hard_fails} >= {"same_template", "thread_arranged", "named_count"}
    plan = CM.thread_repair_plan(tj, [CM.Slot("a", ["补充经验"]), CM.Slot("b", ["补充经验"]), CM.Slot("c", ["提问"])], bad)
    assert {p["slot"] for p in plan} == {"a", "b"}


def test_mcp_comment_tools_mock(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1")
    from judge import mcp_server as M
    r = M.comment_repair_plan_for("标题", "戒烟第三天，嘴里没味，靠嗑瓜子撑着。", "在哪里买的", {"id": "q1", "speech_act": ["提问"], "must_echo": False,
                                  "value_ok": ["可行动信息", "判断依据", "无"], "min_detail": "无"})
    assert set(r) >= {"passed", "plan", "profile", "hard_fails"} and "speech_act" in r["profile"]
    t = M.judge_thread("标题", "戒烟第三天，嘴里没味，靠嗑瓜子撑着。", [{"text": "在哪里买的", "role": "读者位"}, {"text": "药店就有", "role": "贴主", "reply_to_text": "在哪里买的"}])
    assert set(t) >= {"passed", "items", "code", "comments"} and t["code"]["n"] == 2 and "praise_share" in t["items"]
