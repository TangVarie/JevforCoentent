# -*- coding: utf-8 -*-
"""外部笔记的 publish_time 从 note_id 解出来写进表（J-17，TV D-138）。

小红书 note_id 是 24 位十六进制，前 8 位 = 发帖时刻的 unix 秒；TV 书架（librarian/external.py 的 posted_at）和写作台发牌各自现解，
口径一样。以前 external_notes.publish_time 整列是空的（TikHub 搜索页不给）。钉的是：
  · 解法和 TV 同口径（int(前 8 位, 16)）；mock 的 note_id（m 开头）、太短、不是十六进制的解不出 → NULL；
  · 平台给了 publish_time 就照存，不改；没给（None / 空串）才解；
  · 解出来早于 2013 年或晚于抓取 1 天以上 → 当 id 规则变了，留 NULL，不写错的时间；
  · SQL 路径和 PostgREST 行同一个值；
  · 一次性回填脚本默认只看不写，只补仍为空的行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import external as E  # noqa: E402
import scripts.backfill_external_publish_time as BF  # noqa: E402

FETCHED = "2026-11-03T03:09:00+00:00"
NID = "6ae486c0" + "a1b2c3d4e5f60718"            # 前 8 位 = 1793361600 = 2026-10-30 12:00:00 UTC（合成 id）


def _note(**kw):
    n = {"note_id": NID, "category": "戒烟", "keyword": "戒烟", "sort_type": "general", "title": "t", "body": "b",
         "author": "", "author_id": "", "liked": 1, "collected": 0, "comments": 0, "shares": 0, "publish_time": None, "triage": {}}
    n.update(kw)
    return n


def test_same_rule_as_tv_posted_at():
    assert E.publish_time_from_note_id(NID, FETCHED) == "2026-10-30T12:00:00+00:00"
    assert int(NID[:8], 16) == 1793361600
    assert E.publish_time_from_note_id(NID.upper(), FETCHED) == "2026-10-30T12:00:00+00:00"


@pytest.mark.parametrize("nid", ["m1234567890", "6ae486c", "", None, "zzzzzzzz0000000000000000", "6ae4-6c0" + "0" * 16])
def test_unparseable_ids_stay_null(nid):
    assert E.publish_time_from_note_id(nid, FETCHED) is None


def test_out_of_range_stays_null():
    assert E.publish_time_from_note_id("00000001" + "0" * 16, FETCHED) is None               # 1970 年：早于 2013
    assert E.publish_time_from_note_id("ffffffff" + "0" * 16, FETCHED) is None               # 2106 年：晚于抓取
    nxt = f"{1793361600 + 5 * 86400:08x}" + "0" * 16                                         # 抓取之后 4 天多
    assert E.publish_time_from_note_id(nxt, "2026-10-31T12:00:00+00:00") is None
    same_day = f"{1793361600 + 3600:08x}" + "0" * 16                                         # 抓取前后一天内：时钟误差，照写
    assert E.publish_time_from_note_id(same_day, "2026-10-30T12:30:00+00:00") == "2026-10-30T13:00:00+00:00"


def test_rows_fill_publish_time_only_when_not_given():
    rows = E.external_note_rows([_note(), _note(publish_time="1730000000000"), _note(publish_time="  "), _note(note_id="m123456789a")],
                                "ext-t", fetched_at=FETCHED)
    assert [r["publish_time"] for r in rows] == ["2026-10-30T12:00:00+00:00", "1730000000000", "2026-10-30T12:00:00+00:00", None]
    sql = E.rows_to_sql_external(rows[:1])
    assert "'2026-10-30T12:00:00+00:00'" in sql and "publish_time = EXCLUDED.publish_time" in sql


def test_mock_run_rows_unchanged():
    """mock 供应商的 note_id 是 m 开头：解不出，publish_time 照旧 NULL（mock 流程的行不变）。"""
    rows = E.external_note_rows([_note(note_id="m0123456789")], "ext-t", fetched_at=FETCHED)
    assert rows[0]["publish_time"] is None


# ── 一次性回填（默认只看不写）──

def test_backfill_plan_and_sql():
    ok, bad = BF.plan([{"note_id": NID, "fetched_at": FETCHED}, {"note_id": "m123", "fetched_at": FETCHED}])
    assert ok == [(NID, "2026-10-30T12:00:00+00:00")] and bad == ["m123"]
    sql = BF.to_sql(ok)
    assert "e.publish_time IS NULL" in sql and f"('{NID}', '2026-10-30T12:00:00+00:00')" in sql
    assert BF.to_sql([]).startswith("--")


def test_backfill_is_dry_run_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k-test")
    seen = []
    monkeypatch.setattr(BF, "_get", lambda url, key, path: seen.append(path) or [{"note_id": NID, "fetched_at": FETCHED}])

    def no_patch(*a, **k):
        raise AssertionError("dry-run 不许写库")
    monkeypatch.setattr(BF, "_patch", no_patch)
    sql = tmp_path / "bf.sql"
    assert BF.main(["--sql", str(sql)]) == 0
    assert "publish_time=is.null" in seen[0], "只读还空着的行"
    assert "IS NULL" in sql.read_text(encoding="utf-8") and "dry-run" in capsys.readouterr().out


def test_backfill_apply_patches_only_null_rows(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k-test")
    monkeypatch.setattr(BF, "_get", lambda url, key, path: [{"note_id": NID, "fetched_at": FETCHED}, {"note_id": "m1", "fetched_at": FETCHED}])
    patched = []
    monkeypatch.setattr(BF, "_patch", lambda url, key, nid, pt: patched.append((nid, pt)))
    assert BF.main(["--apply"]) == 0
    assert patched == [(NID, "2026-10-30T12:00:00+00:00")], "解不出的不写"


def test_backfill_without_secrets_does_nothing(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False); monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert BF.main([]) == 1
