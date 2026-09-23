#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 external_corpus.py 产出的 rows.json 经 PostgREST upsert 进 TV：先 external_notes（主键 note_id），再 note_feature_answers。
TV 里没有「执行任意 SQL」的 RPC（也不该有），所以走和 core.postgrest_upsert 一样的表级 upsert；没配密钥就打印提示、退出 0，
产物留给人拿 SQL 编辑器跑（answers.sql / external_notes.sql）。
   python3 scripts/apply_rows.py out/rows.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.core import postgrest_upsert  # noqa: E402


def main(argv: list) -> int:
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        print("没有 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY：不写库，rows 留给人经 SQL 编辑器跑")
        return 0
    for f in argv or ["out/rows.json"]:
        p = Path(f)
        if not p.exists():
            print(f"{f}: 不存在（dry-run 或这次没有产物），跳过"); continue
        d = json.loads(p.read_text(encoding="utf-8"))
        n1 = postgrest_upsert(d.get("external_notes") or [], url, key, table="external_notes")
        n2 = postgrest_upsert(d.get("answers") or [], url, key)
        print(f"{f}: external_notes {n1} 行 · note_feature_answers {n2} 行（run {d.get('run_id')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
