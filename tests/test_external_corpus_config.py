# -*- coding: utf-8 -*-
"""config/external_corpus.yaml 的形态守卫（2026-11-02，品类 5 → 8）。

钉的是：
  · 单次硬上限不低于 plan() 算出的最坏花费 —— 低了的话，排在后面的品类每周都可能被预算截掉
    （8 个品类时最坏 4.48 美元，原来的 4.0 就截得到最后一个品类）；
  · 品类名不重、关键词不重 —— 同一个词挂两个品类，同一批笔记会按先到的品类分诊，另一个品类白付搜索费；
  · 品类清单本身 —— 品类名就是 external_notes.category 的值，TV 的 prompts/external_shelf_v1.json（外部参考书架）
    按它一字不差地对表；这里改了品类，那边要补同名一行，所以清单在这里写死，改的人一定会看到这句提醒；
  · 用默认配置整份 mock 跑一遍，每个品类都搜到、都入账本、都带着自己的品类名进外部笔记表的行；
  · 每月最坏花费按「一个月最多几次定时跑」× 单次最坏算（每周一跑 → 最多 5 次，2026-11 就有 5 个周一），
    配置注释 / README / docs/00 #11 写的数要和它对得上：每月 160 篇只数入库篇数，不封顶取全文次数，
    所以第五次照样花满单次最坏（以前写成「每月最坏约 19 美元」，那是平均 4.3 周的数，不是最坏）。
"""
from __future__ import annotations

import calendar
import sys
from datetime import date, datetime, timezone
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


WF = ROOT / ".github" / "workflows" / "external-corpus.yml"


def _max_scheduled_runs_per_month() -> tuple[int, int]:
    """workflow 的 cron 在一个自然月里最多 / 2026-11 各触发几次（只支持「固定分时、按星期几」的形态，别的形态让测试先红）。"""
    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    on = wf[True] if True in wf else wf["on"]               # YAML 1.1 把 on 读成 True
    crons = [x["cron"] for x in on["schedule"]]
    assert len(crons) == 1
    minute, hour, dom, mon, dow = crons[0].split()
    assert minute.isdigit() and hour.isdigit() and dom == "*" and mon == "*", (
        f"cron 变成了 {crons[0]}：每月最多跑几次要重算，配置注释 / README / docs/00 #11 的每月最坏花费跟着改")
    days = {int(d) % 7 for d in dow.split(",")}             # cron 的星期：0 / 7 = 周日，1 = 周一

    def runs(y: int, m: int) -> int:
        return sum(1 for d in range(1, calendar.monthrange(y, m)[1] + 1) if date(y, m, d).isoweekday() % 7 in days)

    return max(runs(y, m) for y in range(2026, 2034) for m in range(1, 13)), runs(2026, 11)


def test_monthly_worst_case_counts_every_scheduled_run_in_the_month():
    cfg = _cfg()
    p = X.plan(cfg)
    runs, nov_2026 = _max_scheduled_runs_per_month()
    assert runs == 5 and nov_2026 == 5, "每周一跑：一个月最多 5 个周一（2026-11 的 2、9、16、23、30 日）"
    worst = round(runs * p["worst_case_usd"], 2)
    assert worst == 22.4
    header = (ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8").split("provider:")[0]
    assert f"{worst:.1f} 美元" in header and f"搜索 {runs * p['search_calls']} 页" in header \
        and f"取全文 {runs * p['fetch_cap']:,} 次" in header, "配置注释的每月最坏要按 5 次运行写（640 页搜索 + 1,600 次取全文）"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    decisions = (ROOT / "docs" / "00-decisions.md").read_text(encoding="utf-8")
    row11 = next(line for line in decisions.splitlines() if line.startswith("| 11 |"))
    assert f"{worst:.1f} 美元" in readme and f"{worst:.1f} 美元" in row11
    for where, text in (("config", header), ("README", readme), ("docs/00 #11", row11)):
        assert "最坏约 19" not in text, f"{where} 还把平均每月（约 4.3 周）的 19 美元当最坏写"


def test_monthly_note_cap_does_not_bound_full_text_pulls():
    """每月 160 篇数的是入库篇数：一个品类本月已留 159 篇，只要这次取到的全文都太短、一篇没留，
    它搜的页数、取全文的次数、花的钱就和月初第一次一模一样（单次上限照样用得满）。
    要是哪天改成按取全文次数封顶，这里会红 —— 那时每月最坏可能不再是 5 × 单次最坏，配置注释 / README / docs/00 #11 要一起改。"""
    cfg = dict(_cfg(), min_body_chars=10_000)               # 取到的全文一律算太短：只花钱不入库
    per_run = int(cfg["max_keep_per_category_per_run"]); per_month = int(cfg["max_keep_per_category_per_month"])
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    fq = B.load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1")

    def run(monthly_kept: int):
        budget = E.Budget(limit_usd=float(cfg["budget_usd_per_run"]), price_per_call=float(cfg["price_per_call_usd"]))
        month = datetime.now(timezone.utc).isoformat(timespec="seconds")[:7]
        state = {"seen": {}, "monthly": {month: {c: monthly_kept for c in CATEGORIES}}}
        rep = E.run_once(cfg, E.TikHubClient(budget=budget, mock=True), JevClient(mock=True), triage, fq, state)
        assert rep.stopped_reason == "" and rep.kept_notes == []
        assert state["monthly"][month] == {c: monthly_kept for c in CATEGORIES}
        return rep

    fresh, nearly_full = run(0), run(per_month - 1)
    pulls = {c: s["detail_calls"] for c, s in nearly_full.per_category.items()}
    assert pulls == {c: s["detail_calls"] for c, s in fresh.per_category.items()}
    assert {c: s["searched"] for c, s in nearly_full.per_category.items()} == {c: s["searched"] for c, s in fresh.per_category.items()}
    assert nearly_full.spent_usd == fresh.spent_usd > 0
    assert per_run in pulls.values(), f"至少一个品类要把单次 {per_run} 次取全文用满，才说明月上限没拦住：{pulls}"
