#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 TV 里【账本写齐了】的外部笔记 id 列出来（一行一个），给 external_corpus.py --known-ids 做第二道去重。

为什么要第二道：state 文件只靠 actions/cache 续命，GitHub 会淘汰 7 天没访问的缓存，周更正好卡在线上，cron 一延迟 state 就归零，
归零那周会把看过的笔记再付一遍钱。库里的 external_notes 不会丢。

为什么只认"账本写齐了"的（codex review on #6，P1）：apply_rows 先写 external_notes 再写账本；账本那一步失败时 job 红、cache 不存，
但 external_notes 里已经有这批笔记了 —— 要是 known_ids 照 external_notes 全收，下周这批按 known 跳过、永远不再判，账本那一半
就永久丢了。所以判据是 note_feature_answers 里这篇 (subject_type=external_note) 的行数 ≥ fq 题库的题数；
不够的下周重抓重判（多花一次钱，不丢）。账本读不到（TV 还没跑 notes_v1_17）就退回只看 external_notes，并把原因打出来。

  python3 scripts/known_external_ids.py state/known_ids.txt
没配 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY、或表还没建（TV 没跑 notes_v1_18）→ 写一个空文件、打印原因、退出 0：去重退回只靠 state。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

PAGE = 1000
ROOT = Path(__file__).resolve().parent.parent


def _get(url: str, key: str, path: str) -> list:
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _pages(url: str, key: str, base: str) -> list:
    out, offset = [], 0
    while True:
        page = _get(url, key, f"{base}&limit={PAGE}&offset={offset}")
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


def fetch_note_ids(url: str, key: str) -> list:
    return [str(x["note_id"]) for x in _pages(url, key, "external_notes?select=note_id&order=note_id") if x.get("note_id")]


def fetch_ledger_counts(url: str, key: str) -> Counter:
    """每篇外部笔记在账本里有几行（subject_type=external_note）。按主键全序翻页。"""
    rows = _pages(url, key, "note_feature_answers?select=subject_id&subject_type=eq.external_note"
                            "&order=subject_id,question_id,question_version,extractor,run_tag")
    return Counter(str(r["subject_id"]) for r in rows if r.get("subject_id"))


def expected_rows() -> int:
    """一篇该有几行 = fq 题库的题数；题库读不到就按 1（有行就算）。"""
    try:
        sys.path.insert(0, str(ROOT))
        from judge.banks import load_bank
        return max(1, len(load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1").questions))
    except Exception:  # noqa: BLE001
        return 1


def complete_ids(note_ids: list, counts: Counter, need: int) -> tuple[list, list]:
    """(账本写齐的, 没写齐的)。"""
    done = [n for n in note_ids if counts.get(n, 0) >= need]
    short = [n for n in note_ids if counts.get(n, 0) < need]
    return done, short


def main(argv: list) -> int:
    out = Path(argv[0] if argv else "state/known_ids.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        out.write_text("", encoding="utf-8")
        print("没有 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY：known_ids 为空，去重只靠 state")
        return 0
    try:
        ids = fetch_note_ids(url, key)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        out.write_text("", encoding="utf-8")
        print(f"读 external_notes 失败（{exc}）：known_ids 为空，去重只靠 state。多半是 TV 还没跑 notes_v1_18")
        return 0
    try:
        counts = fetch_ledger_counts(url, key)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        # 账本读不到 → 退回只看 external_notes。这周写库也会失败（job 红），修好账本之后下一次就按账本判了。
        out.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
        print(f"::warning::读 note_feature_answers 失败（{exc}）：known_ids 退回按 external_notes 全收 {len(ids)} 篇（账本没写齐的这周不会重判）")
        return 0
    need = expected_rows()
    done, short = complete_ids(ids, counts, need)
    out.write_text("\n".join(done) + ("\n" if done else ""), encoding="utf-8")
    print(f"已入库的外部笔记 {len(ids)} 篇，账本写齐（≥{need} 行）的 {len(done)} 篇 → {out}；没写齐的 {len(short)} 篇下周重抓重判")
    if short:
        print("  没写齐的前几篇:", ", ".join(short[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
