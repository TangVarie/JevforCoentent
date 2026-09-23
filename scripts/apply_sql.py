#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把脚本生成的 SQL 经 Supabase 的 SQL 端点执行（需要 service key）。没有 service key 时把文件交给人经 MCP / psql 跑。
   python3 scripts/apply_sql.py a.sql b.sql
"""
import json, os, sys, urllib.request
from pathlib import Path

url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
if not (url and key):
    sys.exit("没有 SUPABASE_URL / SUPABASE_SERVICE_KEY，SQL 留给人跑")
for f in sys.argv[1:]:
    sql = Path(f).read_text(encoding="utf-8")
    if sql.startswith("-- no rows"):
        print(f"{f}: no rows"); continue
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/rpc/exec_sql", data=json.dumps({"query": sql}).encode("utf-8"), method="POST",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            print(f"{f}: HTTP {r.status}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"{f}: 执行失败 {exc}。TV 侧若没有 exec_sql 这个 RPC，请用 psql 跑这个文件。")
