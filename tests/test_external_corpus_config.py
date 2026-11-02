# -*- coding: utf-8 -*-
"""config/external_corpus.yaml 的形态守卫（2026-11-02，品类 5 → 8）。

钉的是：
  · 单次硬上限不低于 plan() 算出的最坏花费 —— 低了的话，排在后面的品类每周都可能被预算截掉
    （8 个品类时最坏 4.48 美元，原来的 4.0 就截得到最后一个品类）；
  · 品类名不重、关键词不重 —— 同一个词挂两个品类，同一批笔记会按先到的品类分诊，另一个品类白付搜索费；
  · 品类清单本身 —— 品类名就是 external_notes.category 的值，TV 的 prompts/external_shelf_v1.json（外部参考书架）
    按它一字不差地对表；这里改了品类，那边要补同名一行，所以清单在这里写死，改的人一定会看到这句提醒；
  · 用默认配置整份 mock 跑一遍，每个品类都搜到、都入账本、都带着自己的品类名进外部笔记表的行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.external_corpus as X  # noqa: E402
from judge import banks as B  # noqa: E402
from judge import external as E  # noqa: E402
from judge.jev_client import JevClient  # noqa: E402

CATEGORIES = ["戒烟", "鼻炎", "运动补给", "术后营养", "求职留学", "家居家电", "个护", "藻油"]


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))


def test_budget_covers_worst_case():
    cfg = _cfg()
    p = X.plan(cfg)
    assert p["categories"] == len(CATEGORIES)
    assert p["search_calls"] == len(CATEGORIES) * 4 * 2 * 2 and p["fetch_cap"] == len(CATEGORIES) * 40
    assert p["worst_case_usd"] <= float(cfg["budget_usd_per_run"]), (
        f"budget_usd_per_run={cfg['budget_usd_per_run']} 低于最坏花费 {p['worst_case_usd']}：排在后面的品类会被预算截掉")


def test_categories_are_the_registered_list_and_keywords_do_not_overlap():
    cats = _cfg()["categories"]
    names = [c["name"] for c in cats]
    assert names == CATEGORIES, (
        "品类变了：TV 的 prompts/external_shelf_v1.json 要按新的品类名一字不差地补 / 改一行（外部参考书架按 category 对表），"
        "再改这里的 CATEGORIES")
    kws = [k for c in cats for k in c["keywords"]]
    assert all(len(c["keywords"]) == 4 for c in cats), "每个品类 4 个关键词（plan 的算式和配置注释按 4 算）"
    assert len(kws) == len(set(kws)), "同一个关键词挂了两个品类"
    assert all(isinstance(k, str) and k.strip() == k and k for k in kws)


def test_default_config_mock_run_reaches_every_category():
    cfg = _cfg()
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    fq = B.load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1")
    budget = E.Budget(limit_usd=float(cfg["budget_usd_per_run"]), price_per_call=float(cfg["price_per_call_usd"]))
    rep = E.run_once(cfg, E.TikHubClient(budget=budget, mock=True), JevClient(mock=True), triage, fq, {"seen": {}, "monthly": {}})
    assert rep.stopped_reason == "" and rep.systemic_failure == ""
    assert list(rep.per_category) == CATEGORIES
    assert all(s["searched"] > 0 and s["judged"] > 0 for s in rep.per_category.values())
    assert rep.spent_usd <= float(cfg["budget_usd_per_run"])
    rows = E.external_note_rows(rep.kept_notes, "ext-test", fetched_at="2026-11-02T00:00:00+00:00")
    assert {r["category"] for r in rows} == set(CATEGORIES)
