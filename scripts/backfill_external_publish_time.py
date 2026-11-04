#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性回填 truth_vault.external_notes.publish_time（J-17）：2026-11-04 之前入库的外部笔记这一列全空，按 note_id 前 8 位十六进制解出发帖时刻补上。

默认只读（dry-run）：读出 publish_time 为空的行，算出每篇的发帖时间，报能补几篇、解不出几篇，不写库。
  python3 scripts/backfill_external_publish_time.py                  # 只看（读库要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）
  python3 scripts/backfill_external_publish_time.py --sql out.sql    # 另写一份 UPDATE，给人在 SQL editor 里跑
  python3 scripts/backfill_external_publish_time.py --apply          # 真写：逐篇 PATCH
只补空的：已有值的行不动；UPDATE / PATCH 都带「publish_time 为空」的条件，重跑不重复、不覆盖。
解法和写入新行时同一个函数（judge.external.publish_time_from_note_id）：解不出、或解出来早于 2013 年 / 晚于抓取 1 天以上的，留空。
TV 书架和写作台发牌照旧自己从 note_id 现解，不读这一列；这一列补上只是让人在 SQL 里能直接看。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.external import publish_time_from_note_id  # noqa: E402

PAGE = 1000
TABLE = "external_notes"


def _headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault", "Content-Profile": "truth_vault"}


def _get(url: str, key: str, path: str) -> list:
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{path}", headers=_headers(key))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _patch(url: str, key: str, note_id: str, publish_time: str) -> None:
    q = f"note_id=eq.{urllib.parse.quote(note_id, safe='')}&publish_time=is.null"
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{TABLE}?{q}", method="PATCH",
                                 data=json.dumps({"publish_time": publish_time}).encode("utf-8"),
                                 headers=dict(_headers(key), **{"Content-Type": "application/json", "Prefer": "return=minimal"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"PostgREST HTTP {r.status}")


def fetch_missing(url: str, key: str) -> list:
    """publish_time 为空的行（note_id, fetched_at），按 note_id 全序翻页。"""
    out, offset = [], 0
    while True:
        page = _get(url, key, f"{TABLE}?select=note_id,fetched_at&publish_time=is.null&order=note_id&limit={PAGE}&offset={offset}")
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


def plan(rows: list) -> tuple[list, list]:
    """(能补的 [(note_id, publish_time)], 解不出的 [note_id])。"""
    ok, bad = [], []
    for r in rows:
        nid = str(r.get("note_id") or "")
        pt = publish_time_from_note_id(nid, r.get("fetched_at"))
        if pt:
            ok.append((nid, pt))
        else:
            bad.append(nid)
    return ok, bad


def to_sql(updates: list, batch: int = 500) -> str:
    """给 SQL editor 的 UPDATE：只改 publish_time 仍为空的行，重跑无副作用。"""
    if not updates:
        return "-- nothing to backfill\n"

    def lit(v: str) -> str:
        return "'" + str(v).replace("'", "''") + "'"
    out = []
    for i in range(0, len(updates), batch):
        vals = ",\n".join(f"({lit(n)}, {lit(t)})" for n, t in updates[i:i + batch])
        out.append(f"UPDATE truth_vault.{TABLE} AS e SET publish_time = v.publish_time\nFROM (VALUES\n{vals}\n) AS v(note_id, publish_time)\n"
                   "WHERE e.note_id = v.note_id AND e.publish_time IS NULL;\n")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="回填 external_notes.publish_time（默认只看不写）")
    ap.add_argument("--apply", action="store_true", help="真写库（逐篇 PATCH，只改仍为空的行）；不带就只看")
    ap.add_argument("--sql", help="另写一份 UPDATE 到这个文件，给人在 SQL editor 里跑")
    args = ap.parse_args(argv)
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        print("没有 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY：读不了库，什么都没做", file=sys.stderr)
        return 1
    try:
        rows = fetch_missing(url, key)
    except Exception as exc:  # noqa: BLE001
        print(f"读 {TABLE} 失败：{str(exc).replace(key, '***REDACTED***')}", file=sys.stderr)
        return 1
    updates, bad = plan(rows)
    print(f"publish_time 为空的外部笔记 {len(rows)} 篇：能从 note_id 解出 {len(updates)} 篇，解不出 {len(bad)} 篇（留空）")
    if updates:
        print("  前几篇:", "; ".join(f"{n[:8]}… → {t}" for n, t in updates[:5]))
    if args.sql:
        Path(args.sql).write_text(to_sql(updates), encoding="utf-8")
        print(f"UPDATE 写到 {args.sql}")
    if not args.apply:
        print("只看不写（dry-run）；真写带 --apply，或把 --sql 那份拿去 SQL editor 跑")
        return 0
    done = 0
    for nid, pt in updates:
        try:
            _patch(url, key, nid, pt)
        except Exception as exc:  # noqa: BLE001
            print(f"写到第 {done + 1} 篇失败（前 {done} 篇已写）：{str(exc).replace(key, '***REDACTED***')}；重跑会接着补没写的", file=sys.stderr)
            return 1
        done += 1
    print(f"已补 {done} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
