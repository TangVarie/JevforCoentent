# -*- coding: utf-8 -*-
"""11-25 跟进（truth-vault S-18 ③，#40 合并后的三处小修）。全合成样本，零付费调用。

① 是非题的期望答案写成 true / "true" 时，修改单里给的定义是反的：先按「是」/「否」归一再比、再查定义。
② unfilled() 只认英文标识符的占位符之后，{帖子要点} 这种认不出了：另加一条只查模板原文的。
③ JUDGE_TZ 设成空串：#40 之前是 UTC，之后和没设一样是北京时间；以前的测试是 import 之后才设环境变量，没测到这一步，改成直接测。"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import loop as L  # noqa: E402
from judge.jev_client import JevClient  # noqa: E402

AMB = {"choice_top_min": 0.6, "choice_margin_min": 0.15, "noul_band": [0.4, 0.6]}
TRUE_DEF, FALSE_DEF = "开头先讲一件具体的事", "开头平铺直叙"


def _bank(*qs, name="project"):
    return B.Bank(name=name, version="0.1", model="m", fmt="jev", questions=list(qs), ambiguity=AMB, sha256="x")


def _hook():
    return B.Question(id="has_hook", jtype="noul", instructions="开头有没有钩子？", criteria={"true": TRUE_DEF, "false": FALSE_DEF})


def _dj(now: str, wants: dict | None = None, hard: bool = False):
    return L.DraftJudgement(profile={"has_hook": now}, detail={"project": {"has_hook": {"answer": now, "p": 0.9}}},
                            wants=wants or {}, hard_fails=[("project", "has_hook", now, 0.9, None)] if hard else [], calls=1)


# ── ① 是非题的期望答案先归一 ──

@pytest.mark.parametrize("want", [True, "true", "True", " TRUE ", "是"])
def test_noul_target_true_gives_the_true_definition(want):
    plan = L.repair_plan(_dj("否"), {"project": _bank(_hook())}, target={"has_hook": want})
    assert len(plan) == 1, plan
    assert plan[0]["want"] == "是" and plan[0]["definition"] == TRUE_DEF, "以前 true / \"true\" 会查到 false 那条定义"
    assert TRUE_DEF in plan[0]["instruction"] and FALSE_DEF not in plan[0]["instruction"]


@pytest.mark.parametrize("want", [False, "false", "False", "否"])
def test_noul_target_false_gives_the_false_definition(want):
    plan = L.repair_plan(_dj("是"), {"project": _bank(_hook())}, target={"has_hook": want})
    assert [(p["want"], p["definition"]) for p in plan] == [("否", FALSE_DEF)]


@pytest.mark.parametrize("want", [True, "true"])
def test_noul_target_already_met_is_not_a_repair_item(want):
    """已经是「是」、目标写成 true：不进修改单（以前「是」≠ True，会出一条「目标是 True」的修改）。"""
    assert L.repair_plan(_dj("是"), {"project": _bank(_hook())}, target={"has_hook": want}) == []


def test_unrecognised_noul_want_gets_no_definition_rather_than_the_false_one():
    plan = L.repair_plan(_dj("否"), {"project": _bank(_hook())}, target={"has_hook": "也许"})
    assert len(plan) == 1 and plan[0]["definition"] == "", "认不出的期望答案不猜成「否」"


def test_noul_hard_rule_written_as_string_true_gets_the_true_definition():
    dj = _dj("否", wants={("project", "has_hook"): "true"}, hard=True)
    plan = L.repair_plan(dj, {"project": _bank(_hook())})
    assert [(p["kind"], p["want"], p["definition"]) for p in plan] == [("hard", "是", TRUE_DEF)]


def test_choice_targets_are_left_alone():
    q = B.Question(id="opening_type", jtype="choice", instructions="开头是哪一种？", criteria={"true": "选项名恰好叫 true", "具体事件": "讲一件事"})
    bank = _bank(q)
    assert L.normalize_target({"opening_type": "true"}, {"project": bank}) == {"opening_type": "true"}
    dj = L.DraftJudgement(profile={"opening_type": "具体事件"}, detail={"project": {"opening_type": {"answer": "具体事件", "p": 0.9}}}, calls=1)
    plan = L.repair_plan(dj, {"project": bank}, target={"opening_type": "true"})
    assert plan[0]["want"] == "true" and plan[0]["definition"] == "选项名恰好叫 true"


def test_normalize_target_unit():
    banks = {"project": _bank(_hook())}
    assert L.normalize_target({"has_hook": True}, banks) == {"has_hook": "是"}
    assert L.score(_dj("是"), L.normalize_target({"has_hook": True}, banks)) == L.score(_dj("是"), {"has_hook": "是"}) == 0.0
    assert L.normalize_target(None, banks) is None and L.normalize_target({}, banks) == {}


class _FixedJev(JevClient):
    """假 Jev：has_hook 一律答「是」、has_price 一律答「是」（p = 0.95），别的题照 mock。"""

    def __init__(self):
        super().__init__(mock=True)

    def call(self, body):
        r = super().call(body)
        ans = dict(r["answers"])
        for qid in ("has_hook", "has_price"):
            if qid in body["questions"]:
                ans[qid] = {"type": "noul", "noul": 0.95}
        return dict(r, answers=ans)


@pytest.mark.parametrize("want", [True, "true", " TRUE "])
def test_produce_scores_and_plans_against_the_normalised_target(monkeypatch, want):
    """走真的 produce()：两版候选 has_hook 都已经是「是」、target 写成 true / "true"。
    produce 先归一 target（{"has_hook": "是"}）再交给 best_of_k 打分和 repair_plan：两版都是 0 分；
    没归一时「是」≠ True，每版多扣 10 分（排序看的就是这个分）。has_price 是硬伤（want 否、答是），让修补那一步也走到。"""
    seen_best, seen_plan = [], []
    real_best, real_plan = L.best_of_k, L.repair_plan

    def spy_best(*a, **kw):
        seen_best.append(kw.get("target"))
        return real_best(*a, **kw)

    def spy_plan(*a, **kw):
        seen_plan.append(kw.get("target"))
        return real_plan(*a, **kw)

    monkeypatch.setattr(L, "best_of_k", spy_best)
    monkeypatch.setattr(L, "repair_plan", spy_plan)
    price = B.Question(id="has_price", jtype="noul", instructions="有没有提价格？", criteria={"true": "提了价格", "false": "没提价格"})
    body = "上个月报了个游泳班，教练第一天就问我平时是不是熬夜。我说是，他让我先调两周作息再下水。"
    prompt = f"⟪标题：一\n正文：{body}⟫⟪标题：二\n正文：{body}第二版。⟫"
    out = L.produce(_FixedJev(), L.EchoGenerator(), prompt, k=2, max_repairs=1, project=_bank(_hook(), price),
                    hard_rules={("project", "has_price"): "否"}, target={"has_hook": want}, hidden=set())
    assert seen_best == [{"has_hook": "是"}], "produce 交给 best_of_k 的 target 要先归一"
    assert out["trail"][0]["ranking"] == [100.0, 100.0], "只有 has_price 一条硬伤（100）；target 没归一会再加 10"
    assert seen_plan == [{"has_hook": "是"}] and out["trail"][1]["step"] == "repair_1"
    assert [(p["kind"], p["qid"]) for p in out["trail"][1]["plan"]] == [("hard", "has_price")], "has_hook 已经是目标，不进修改单"


def test_hard_rules_with_string_true_false_are_normalised_at_setup():
    """/judge_draft 的 hard_rules 和 brief.hard_rules 写成 "true" / "false" 时也按「是」/「否」比（以前原样留着，答案永远不等、篇篇硬伤）。"""
    pytest.importorskip("fastapi")
    from judge import api as A
    from judge import draft as D

    ds = D.setup_draft(A.get_bank, ["platform_health_v0.1"], project_code="SYNTH", category="合成品类",
                       hard_rules={"platform_health_v0.1:efficacy_claim": "false", "platform_health_v0.1:fear_sell": "TRUE"})
    assert ds.hard[("platform_health_v0.1", "efficacy_claim")] == "否" and ds.hard[("platform_health_v0.1", "fear_sell")] == "是"
    brief = {"hard_rules": [{"id": "r1", "ask": "合成规则？", "want": "true"}, {"id": "r2", "ask": "合成规则二？", "want": "false"},
                            {"id": "r3", "ask": "合成规则三？", "want": True}, {"id": "r4", "ask": "合成规则四？"}]}
    assert L.project_hard_rules(brief) == {("project", "r1"): "是", ("project", "r2"): "否", ("project", "r3"): "是", ("project", "r4"): "否"}


# ── ② unfilled() 另查模板原文 ──

@pytest.mark.parametrize("where", ["instructions", "criteria"])
def test_unfilled_flags_non_identifier_placeholders_in_the_template(where):
    text = "这条评论有没有接住帖子要点：{帖子要点}？"
    if where == "instructions":
        q = B.Question(id="q1", jtype="noul", instructions=text, criteria={"true": "接住了", "false": "没接住"})
    else:
        q = B.Question(id="q1", jtype="noul", instructions="有没有接住帖子要点？", criteria={"true": text, "false": "没接住"})
    bank = _bank(q, name="synthetic_reader")
    assert B.unfilled(bank, {}) == ["q1"]
    assert B.unfilled(bank, {"帖子要点": "合成要点"}) == ["q1"], "fill_text 只填英文标识符：给了同名的值也填不进去，照样算没填"
    built = B.build_questions(bank, fill={"帖子要点": "合成要点"})["q1"]
    assert "{帖子要点}" in built["instructions"] + "".join(built["criteria"].values())


def test_unfilled_still_ignores_braces_inside_the_filled_values():
    q = B.Question(id="q1", jtype="noul", instructions="帖子正文：{post_body}\n要点：{post_points}", criteria={"true": "是", "false": "否"})
    bank = _bank(q, name="synthetic_reader")
    assert B.unfilled(bank, {"post_body": "正文里有 {价格} 和 {成分} 这种花括号", "post_points": "{要点}"}) == []
    assert B.unfilled(bank, {"post_body": "x"}) == ["q1"]


def test_shipped_banks_have_no_unfillable_braces():
    """仓里的题库模板只用英文标识符的占位符：新加的那条不会让现有题库在填齐之后被拦。"""
    for name, path in B.discover(ROOT / "banks").items():
        bank = B.load_bank(path, name=name)
        names = {m.group(1) for q in bank.questions for t in [q.instructions, *map(str, q.criteria.values())]
                 for m in B._PLACEHOLDER_RE.finditer(t)}
        assert B.unfilled(bank, {n: "合成" for n in names}) == [], name


# ── ③ JUDGE_TZ 空串 ──

@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_blank_judge_tz_means_beijing_time(raw):
    from judge import hidden as H
    assert H.tz_name(raw) == "Asia/Shanghai"


def test_judge_tz_name_is_stripped_and_kept():
    from judge import hidden as H
    assert H.tz_name(" UTC ") == "UTC" and H.tz_name("Asia/Shanghai") == "Asia/Shanghai"


def test_blank_judge_tz_at_import_is_beijing_not_utc(monkeypatch):
    """直接测 import 时那一步：JUDGE_TZ="" 时季度按北京时间算（+8）。#40 之前空串走到 ZoneInfo("") 报错、退成 UTC。"""
    import judge.hidden as H
    assert H._load_tz("").utcoffset(datetime(2026, 10, 1)) == timedelta(0), "#40 之前的走法（空串直接当时区名）是 UTC"
    monkeypatch.setenv("JUDGE_TZ", "")
    try:
        importlib.reload(H)
        assert H.QUARTER_TZ_NAME == "Asia/Shanghai"
        assert H.QUARTER_TZ.utcoffset(datetime(2026, 10, 1)) == timedelta(hours=8)
    finally:
        monkeypatch.delenv("JUDGE_TZ", raising=False)
        importlib.reload(H)
    assert H.QUARTER_TZ_NAME == "Asia/Shanghai"
