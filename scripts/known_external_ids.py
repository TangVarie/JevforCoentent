#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 TV 里已经入库的外部笔记 id 列出来（truth_vault.external_notes.note_id，一行一个），给 external_corpus.py --known-ids 做第二道去重。

为什么要第二道：state 文件只靠 actions/cache 续命，GitHub 会淘汰 7 天没访问的缓存，周更正好卡在线上，cron 一延迟 state 就归零，
归零那周会把看过的笔记再付一遍钱。库里的 external_notes 不会丢。

  python3 scripts/known_external_ids.py state/known_ids.txt
没配 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY、或表还没建（TV 没跑 notes_v1_18）→ 写一个空文件、打印原因、退出 0：去重退回只靠 state。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PAGE = 1000


def fetch(url: str, key: str) -> list:
    ids, offset = [], 0
    while True:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/rest/v1/external_notes?select=note_id&order=note_id&limit={PAGE}&offset={offset}",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.loads(r.read().decode("utf-8"))
        ids.extend(str(x["note_id"]) for x in page if x.get("note_id"))
        if len(page) < PAGE:
            return ids
        offset += PAGE


def main(argv: list) -> int:
    out = Path(argv[0] if argv else "state/known_ids.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        out.write_text("", encoding="utf-8")
        print("没有 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY：known_ids 为空，去重只靠 state")
        return 0
    try:
        ids = fetch(url, key)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        out.write_text("", encoding="utf-8")
        print(f"读 external_notes 失败（{exc}）：known_ids 为空，去重只靠 state。多半是 TV 还没跑 notes_v1_18")
        return 0
    out.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
    print(f"已入库的外部笔记 {len(ids)} 篇 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
