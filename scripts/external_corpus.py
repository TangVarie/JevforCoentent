#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部语料 · 定时限额抓取（供应商 TikHub）。量级、频率、预算全在 config/external_corpus.yaml 里。

  python3 scripts/external_corpus.py --dry-run                     # 只算要花多少钱、搜多少页，不联网
  python3 scripts/external_corpus.py --probe --keyword 戒烟          # 花 1 次请求看原始返回，钉字段
  python3 scripts/external_corpus.py --out out/report.md --sql out/answers.sql --notes-sql out/external_notes.sql --rows out/rows.json
  python3 scripts/external_corpus.py --mock ...                    # 假供应商 + 假 Jev，跑通流程；state 默认落到临时目录，不碰仓里的

产物：运行报告（md）、账本 SQL（note_feature_answers，subject_type = external_note，run_tag = external）、
外部笔记表 SQL（truth_vault.external_notes，migrations/notes_v1_18）、rows.json（两张表的行，给 scripts/apply_rows.py 经 PostgREST 写库）、
state/external_corpus_state.json（去重与月度计数）。
定时：.github/workflows/external-corpus.yml 每周一跑一次；改频率改 cron，改量级改 config。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import load_bank  # noqa: E402
from judge.core import rows_to_sql  # noqa: E402
from judge.external import Budget, TikHubClient, external_note_rows, load_state, render_report, rows_to_sql_external, run_once, save_state  # noqa: E402
from judge.jev_client import JevClient, JevError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config" / "external_corpus.yaml"
STATE = ROOT / "state" / "external_corpus_state.json"

def mock_state_path() -> Path:
    """--mock 每次一个新的临时 state 文件：假供应商的 note_id 是确定性的，复用同一份 state 第二次跑就全是重复、零产物。"""
    fd, name = tempfile.mkstemp(prefix="judge_external_corpus_mock_", suffix=".json"); os.close(fd)
    return Path(name)


def plan(cfg: dict) -> dict:
    cats = cfg.get("categories") or []
    searches = sum(len(c.get("keywords") or []) for c in cats) * len(cfg.get("sorts") or [1]) * int(cfg.get("pages_per_sort", 1))
    fetch_cap = len(cats) * int(cfg.get("max_keep_per_category_per_run", 40))
    price = float(cfg.get("price_per_call_usd", 0.01))
    return {"categories": len(cats), "search_calls": searches, "fetch_cap": fetch_cap,
            "worst_case_usd": round((searches + fetch_cap) * price, 2), "budget_usd": cfg.get("budget_usd_per_run"),
            "monthly_notes_cap": len(cats) * int(cfg.get("max_keep_per_category_per_month", 160))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--state", help=f"去重状态文件；默认 {STATE}，--mock 时默认每次新建一个临时文件（不污染真实 state）")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--probe", action="store_true"); ap.add_argument("--keyword", default="戒烟")
    ap.add_argument("--mock", action="store_true"); ap.add_argument("--out"); ap.add_argument("--sql"); ap.add_argument("--notes-sql"); ap.add_argument("--raw")
    ap.add_argument("--rows", help="两张表的行写成 JSON（{run_id, external_notes, answers}），给 scripts/apply_rows.py 经 PostgREST 写库")
    ap.add_argument("--known-ids", help="已在账本里的 external note_id 列表文件（一行一个），用于去重")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    p = plan(cfg)
    print(f"计划：{p['categories']} 个品类，搜索 {p['search_calls']} 页，最多取全文 {p['fetch_cap']} 篇，最坏花费 {p['worst_case_usd']} 美元（上限 {p['budget_usd']}），本月最多留 {p['monthly_notes_cap']} 篇")
    if args.dry_run and not args.mock:
        return
    state_path = Path(args.state) if args.state else (mock_state_path() if args.mock else STATE)
    if args.mock and not args.state:
        print(f"mock：state 落到 {state_path}（每次新建）")
    budget = Budget(limit_usd=float(cfg.get("budget_usd_per_run", 1.0)), price_per_call=float(cfg.get("price_per_call_usd", 0.01)))
    try:
        provider = TikHubClient(budget=budget, mock=args.mock)
    except RuntimeError as exc:
        sys.exit(str(exc))
    if args.probe:
        notes, raw = provider.search(args.keyword, 1, "general", cfg.get("note_type"))
        print(json.dumps(raw, ensure_ascii=False)[:3000]); print(f"\n识别出 {len(notes)} 条笔记；花费 {budget.spent} 美元"); return
    try:
        jev = JevClient(mock=args.mock)
    except JevError as exc:
        sys.exit(str(exc))
    triage = load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    fq = load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1")
    state = load_state(state_path)
    known = set(Path(args.known_ids).read_text(encoding="utf-8").split()) if args.known_ids else set()
    rep = run_once(cfg, provider, jev, triage, fq, state, known_ids=known, dry_run=args.dry_run)
    if not args.dry_run:
        save_state(state_path, state)
    run_id = "ext-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    report = render_report(rep, cfg)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    note_rows = external_note_rows(rep.kept_notes, run_id)
    if args.sql:
        Path(args.sql).write_text(rows_to_sql(rep.rows), encoding="utf-8")
    if args.notes_sql:
        Path(args.notes_sql).write_text(rows_to_sql_external(note_rows), encoding="utf-8")
    if args.rows:
        Path(args.rows).write_text(json.dumps({"run_id": run_id, "external_notes": note_rows, "answers": rep.rows}, ensure_ascii=False), encoding="utf-8")
    if args.raw:
        Path(args.raw).write_text(json.dumps({"run_id": run_id, "kept": rep.kept_notes}, ensure_ascii=False, indent=1), encoding="utf-8")
    if rep.stopped_reason:
        # 只告警不改退出码：job 一红，actions/cache 就不保存 state、写库步也被跳过，这周花钱看过的下周会重看。
        print(f"::warning::运行提前停止：{rep.stopped_reason}（产物与 state 已写出）", file=sys.stderr)


if __name__ == "__main__":
    main()
