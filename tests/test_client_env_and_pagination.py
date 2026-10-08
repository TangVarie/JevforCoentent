# -*- coding: utf-8 -*-
"""Jev 客户端的 timeout / retries 能从 env 调 (TV 审计 B-08) · 评论回填的取数会翻页 (B-09)。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge.jev_client import JevClient  # noqa: E402


def test_client_defaults_unchanged_without_env(monkeypatch):
    monkeypatch.delenv("JEV_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("JEV_RETRIES", raising=False)
    c = JevClient(mock=True)
    assert c.timeout == 30.0 and c.retries == 3


def test_client_reads_env_and_clamps(monkeypatch):
    monkeypatch.setenv("JEV_TIMEOUT_SEC", "6")
    monkeypatch.setenv("JEV_RETRIES", "1")
    c = JevClient(mock=True)
    assert c.timeout == 6.0 and c.retries == 1, "写作台那种部署: 一次 429 不能把 8 秒预算吃光"
    monkeypatch.setenv("JEV_TIMEOUT_SEC", "9999")
    monkeypatch.setenv("JEV_RETRIES", "nan")
    c = JevClient(mock=True)
    assert c.timeout == 120.0 and c.retries == 3, "越界夹到界内, 写坏退回默认, 不让 import / 启动炸"
    c = JevClient(mock=True, timeout=5, retries=0)
    assert c.timeout == 5.0 and c.retries == 0, "显式传参永远压过 env"


def test_backfill_fetch_pages_past_max_rows(monkeypatch):
    import scripts.backfill_comments as B
    rows = [{"comment_id": f"c{i}", "note_id": f"n{i // 3}", "content": "x", "comment_role": "素人", "is_pinned": False}
            for i in range(2500)]
    calls: list[str] = []

    def fake_pg(url, key, path):
        calls.append(path)
        if path.startswith("comments?"):
            q = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
            limit, offset = int(q["limit"]), int(q.get("offset", 0))
            return rows[offset: offset + min(limit, 1000)]          # 服务端钳到 1000
        return [{"note_id": nid, "title": "t", "raw_content": "body"} for nid in path.split("in.(")[1].rstrip(")").split(",")]
    monkeypatch.setattr(B, "_pg", fake_pg)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    got = B.fetch(limit=9652, project=None)
    assert len(got) == 2500, f"要翻页拿全 2500 条, 实际 {len(got)} (单次 GET 会被钳到 1000)"
    pages = [p for p in calls if p.startswith("comments?")]
    assert len(pages) == 3 and "offset=1000" in pages[1] and "offset=2000" in pages[2], pages
    assert all("order=note_id,comment_order,comment_id" in p for p in pages), "翻页要有稳定的全序, 否则页间会重复 / 漏行"
    calls.clear()
    got = B.fetch(limit=1500, project="P")
    assert len(got) == 1500, f"--limit 1500 要恰好停在 1500, 实际 {len(got)}"
    pages = [p for p in calls if p.startswith("comments?")]
    assert len(pages) == 2 and "limit=500" in pages[1], "第二页只要 500 条 (limit 减去已拿的)"
    assert all("note_id=like.P*" in p for p in pages), "--project 过滤要带到每一页, 不能只带第一页"


def test_backfill_fetch_stops_on_short_page(monkeypatch):
    import scripts.backfill_comments as B
    rows = [{"comment_id": f"c{i}", "note_id": "n0", "content": "x", "comment_role": "素人", "is_pinned": False}
            for i in range(7)]
    calls: list[str] = []

    def fake_pg(url, key, path):
        calls.append(path)
        if path.startswith("comments?"):
            q = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
            limit, offset = int(q["limit"]), int(q.get("offset", 0))
            return rows[offset: offset + limit]
        return [{"note_id": "n0", "title": "t", "raw_content": "body"}]
    monkeypatch.setattr(B, "_pg", fake_pg)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    got = B.fetch(limit=500, project=None)
    assert len(got) == 7 and len([p for p in calls if p.startswith("comments?")]) == 1, \
        "短页 (7 < 500) 就是最后一页, 不该再发第二个 GET 去确认空页"
