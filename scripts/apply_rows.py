#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 external_corpus.py 产出的 rows.json 经 PostgREST upsert 进 TV：先 external_notes（主键 note_id），再 note_feature_answers。
TV 里没有「执行任意 SQL」的 RPC（也不该有），所以走和 core.postgrest_upsert 一样的表级 upsert。
没配密钥：本机跑打印提示、退出 0，产物留给人拿 SQL 编辑器跑（answers.sql / external_notes.sql）；
在 GitHub Actions 里（GITHUB_ACTIONS=true）退 1、job 红（2026-11-04，审计 11-02 F16）：以前这里也退 0，job 照绿、cache 把
seen state 存下来，下周这批被跳过，只剩 artifact 能手动重放。
--check-env：只查 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 在不在（不联网、不打印值），缺就退 1。workflow 在任何付费调用
（TikHub 搜索 / 取全文、Jev 判定）之前先跑这一步，缺密钥一分钱都不花就红。
前置：TV 已跑 migrations/notes_v1_17（subject_type 放开 external_note）与 notes_v1_18（external_notes 表）；没跑的话账本那一步会被
CHECK 约束拒绝（PostgREST 400），本脚本会把它打出来并以非零退出。
⚠️ workflow 里这一步【会】拖红 job（2026-10-08 起，TV 审计 A-08）：run 步在写库之前就把 seen state 存好了，写库失败还让 job 绿的话
actions/cache 会把那份 state 保存下来，账本里却没有这批笔记 —— 下周按 seen 跳过、永远不再抓。job 红 = cache 不保存 = 下周重抓。
⚠️ 两张表之间也没有事务：external_notes 写成、账本写炸的话，这批笔记在 external_notes 里有行、账本里没有。known_external_ids.py
只把【账本写齐的】算 known（codex review on #6），所以这批下周会重抓重判，不会因为"在 external_notes 里"被跳过。
   python3 scripts/apply_rows.py out/rows.json
   python3 scripts/apply_rows.py --check-env
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.core import postgrest_upsert  # noqa: E402


DB_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")


def missing_db_env() -> list:
    """缺的写库变量名（只看在不在，不碰值）。"""
    return [k for k in DB_ENV if not os.environ.get(k, "").strip()]


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def main(argv: list) -> int:
    argv = list(argv)
    if argv[:1] == ["--check-env"]:
        miss = missing_db_env()
        if miss:
            print(f"::error::缺 {' / '.join(miss)}：外部语料抓完写不进库（下周这批会被 seen state 跳过）。"
                  "先在仓库的 Actions secrets 里配上，再手动触发；这次一次付费调用都没发", file=sys.stderr)
            return 1
        print("写库密钥齐了（SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）")
        return 0
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    miss = missing_db_env()
    if miss:
        if in_github_actions():
            print(f"::error::缺 {' / '.join(miss)}：不写库。rows 在这次的 artifact 里，配好密钥后手动跑本脚本补写", file=sys.stderr)
            return 1
        print("没有 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY：不写库，rows 留给人经 SQL 编辑器跑")
        return 0
    for f in argv or ["out/rows.json"]:
        p = Path(f)
        if not p.exists():
            print(f"{f}: 不存在（dry-run 或这次没有产物），跳过"); continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            print(f"{f}: 不是 external_corpus.py --rows 产出的形状（要 {{run_id, external_notes, answers}}），跳过"); continue
        try:
            n1 = postgrest_upsert(d.get("external_notes") or [], url, key, table="external_notes")
            n2 = postgrest_upsert(d.get("answers") or [], url, key)
        except Exception as exc:  # noqa: BLE001
            # 异常文本里万一带着 key (urllib 的 URLError 会回显请求头? 不会, 但 TV 审计 B-11 要求日志出口不赌) → 抹掉
            msg = str(exc).replace(key, "***REDACTED***")
            print(f"{f}: 写库失败：{msg}。先确认 TV 已跑 notes_v1_17 / notes_v1_18；产物仍在 artifact 里可手工执行", file=sys.stderr)
            return 1
        print(f"{f}: external_notes {n1} 行 · note_feature_answers {n2} 行（run {d.get('run_id')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
