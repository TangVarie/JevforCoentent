# -*- coding: utf-8 -*-
"""账本写到一半失败要说清写了多少, 且不把已付钱的判定整个变成 500 (TV 审计 2026-10-08 B-06)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import core as C  # noqa: E402

BODY = ("昨天在药店买了一盒东西，嚼了几口辣嗓子，有点想戒了。后来朋友说换个口味试试，我就又买了一盒薄荷的。"
        "说实话效果一般，但比干熬着强。大家平时都怎么扛过去的？评论区聊聊。") * 3


def _rows(n: int) -> list:
    return [{"subject_id": f"s{i}", "extractor": "jev:1.13.0", "question_id": "q"} for i in range(n)]


def test_partial_write_reports_written_total_and_batch(monkeypatch):
    import urllib.request
    calls: list[int] = []

    class Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=60):
        calls.append(len(json.loads(req.data)))
        if len(calls) == 2:
            raise OSError("connection reset")          # 第 2 批炸
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(C.LedgerWriteError) as ei:
        C.postgrest_upsert(_rows(500), "http://h", "k", batch=200)
    e = ei.value
    assert (e.written, e.total, e.failed_batch, e.uncertain) == (200, 500, 2, 200), \
        "第 1 批 200 行确认在库里, 第 2 批 (200 行) 状态不明, 第 3 批没发"
    assert calls == [200, 200], "炸了就停, 不该继续发第 3 batch"
    assert "200/500" in str(e) and "幂等" in str(e)
    assert "状态不明" in str(e) and "100 行没发" in str(e), "炸掉那批可能已提交, 不能报成'没写' (codex review on #6)"
    assert isinstance(e, RuntimeError), "老调用方 except RuntimeError / Exception 照样接得住"


def test_judge_draft_returns_200_with_write_error_when_ledger_half_written(monkeypatch, policy_cfg):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge import api as A
    monkeypatch.setattr(A, "_write_config_or_503", lambda: None)

    def half(rows):
        raise C.LedgerWriteError(7, len(rows), 1, OSError("boom"))
    monkeypatch.setattr(A, "_write_rows", half)
    c = TestClient(A.app)
    r = c.post("/judge_draft", json={"title": "t", "body": BODY, "subject_id": "v9", "project": "NRT", "write": True},
               headers={"X-Judge-Key": "k"})
    assert r.status_code == 200, "Jev 的钱付了、答案算出来了, 账本半写是下游的事, 不该 500"
    d = r.json()
    assert d["written"] == 7 and d["rows"] > 7, "written 是真写进去的行数, 不是打算写的"
    assert "7/" in d["write_error"] and "第 1 批" in d["write_error"]
    assert d["ledger_rows"] is not None and d["passed"] is not None, "判定结果照常回去"


def test_judge_returns_rows_for_rewrite_when_write_fails_even_if_not_requested(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge import api as A
    monkeypatch.setattr(A, "_write_config_or_503", lambda: None)
    monkeypatch.setattr(A, "_write_rows", lambda rows: (_ for _ in ()).throw(C.LedgerWriteError(0, len(rows), 1, OSError("x"))))
    c = TestClient(A.app)
    body = {"bank": "feature_questions_v0_1", "run_tag": "shadow-t", "write": True, "return_rows": False, "subjects": [
        {"subject_type": "note", "subject_id": "n1", "raw_content": "标题：测试标题？\n正文：" + BODY}]}
    r = c.post("/judge", json=body, headers={"X-Judge-Key": "k"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["written"] == 0 and d["write_error"] and d["ledger_rows"] and len(d["ledger_rows"]) == d["rows"], \
        "写失败时 return_rows=false 也要把行回去, 调用方才能补写"
    # 写成功: 没有 write_error, return_rows=false 照旧不回行
    monkeypatch.setattr(A, "_write_rows", lambda rows: len(rows))
    d2 = c.post("/judge", json=body, headers={"X-Judge-Key": "k"}).json()
    assert d2["written"] == d2["rows"] and d2["write_error"] is None and d2["ledger_rows"] is None


def test_missing_write_config_is_still_503(monkeypatch):
    # 不开 mock: mock 模式下 write=true 先被 422 挡住 (假答案不许进账本), 测不到 503 这一层
    monkeypatch.delenv("JUDGE_MOCK", raising=False); monkeypatch.setenv("JUDGE_API_KEY", "k")
    monkeypatch.delenv("SUPABASE_URL", raising=False); monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    from fastapi.testclient import TestClient
    from judge import api as A
    c = TestClient(A.app)
    body = {"bank": "feature_questions_v0_1", "write": True, "with_evidence": True, "subjects": [
        {"subject_type": "note", "subject_id": "n1", "raw_content": "标题：x\n正文：" + BODY}]}
    r = c.post("/judge", json=body, headers={"X-Judge-Key": "k"})
    assert r.status_code == 503, "没配密钥是配置问题, 照旧 503, 不是半写"


# ── 批按 subject 边界切 (2026-11-15 第三轮外部评审 E) ───────────────────────────────────────

def _subject_rows(subject_id: str, n: int, subject_type: str = "aw_version") -> list:
    return [{"subject_type": subject_type, "subject_id": subject_id, "question_id": f"q{i}", "extractor": "jev:1.13.0"}
            for i in range(n)]


def _draft(subject_id: str, own: int, paras: int, per_para: int) -> list:
    rows = _subject_rows(subject_id, own)
    for p in range(1, paras + 1):
        rows += _subject_rows(f"{subject_id}:p{p}", per_para)
    return rows


def _family(r: dict) -> str:
    return r["subject_id"].split(":p")[0]


def test_batches_never_split_a_subject_and_keep_a_draft_with_its_paragraphs():
    rows = _draft("v1", 20, 6, 5) + _draft("v2", 20, 6, 5) + _draft("v3", 20, 6, 5)      # 每篇 50 行
    parts = C.subject_batches(rows, batch=120)
    assert [len(p) for p in parts] == [100, 50], "第三篇装不进第一批就整篇去下一批, 不从中间切"
    assert [sorted({_family(r) for r in p}) for p in parts] == [["v1", "v2"], ["v3"]]
    assert sorted(map(id, (r for p in parts for r in p))) == sorted(map(id, rows)), "行不丢不重"
    for p in parts:                                                                        # 同一 subject 内顺序不变
        for sid in {r["subject_id"] for r in p}:
            mine = [r["question_id"] for r in p if r["subject_id"] == sid]
            assert mine == [f"q{i}" for i in range(len(mine))]


def test_batches_group_rows_of_one_subject_that_arrive_apart():
    """两套题库的行交错着来 (先全部稿子的 A 题库, 再全部稿子的 B 题库): 同一 subject 仍然落在同一批。"""
    a = [r for s in ("c1", "c2", "c3") for r in _subject_rows(s, 3, "comment")]
    b = [dict(r, question_id="b" + r["question_id"]) for r in a]
    parts = C.subject_batches(a + b, batch=12)
    assert [len(p) for p in parts] == [12, 6]
    homes = {}
    for k, p in enumerate(parts):
        for r in p:
            homes.setdefault(r["subject_id"], set()).add(k)
    assert all(len(v) == 1 for v in homes.values()), homes


def test_draft_larger_than_one_batch_splits_between_subjects_only():
    rows = _draft("v1", 20, 8, 5)                       # 60 行, 一批只放 32
    parts = C.subject_batches(rows, batch=32)
    assert all(len(p) <= 32 for p in parts)
    seen = {}
    for k, p in enumerate(parts):
        for r in p:
            seen.setdefault(r["subject_id"], set()).add(k)
    assert all(len(v) == 1 for v in seen.values()), "稿子本身和每一段各自完整, 只在段与段之间切"
    assert sum(map(len, parts)) == 60


def test_single_subject_over_the_batch_size_still_goes_out_in_bounded_requests():
    parts = C.subject_batches(_subject_rows("big", 25), batch=10)
    assert [len(p) for p in parts] == [10, 10, 5], "单个 subject 超过一批是唯一按行数切的情况, 请求大小仍有上限"


def test_rows_without_subject_id_keep_fixed_size_batches():
    rows = [{"note_id": f"x{i}", "fetched_at": "2026-09-24"} for i in range(25)]      # external_notes 的形状
    assert [len(p) for p in C.subject_batches(rows, batch=10)] == [10, 10, 5]
    assert C.subject_batches([], batch=10) == []


def test_failure_between_batches_leaves_no_half_written_subject(monkeypatch):
    """以前固定 200 行一刀: 第 2 批失败时, 跨在刀口上的那篇稿子前一半题已经在库里、后一半没写。"""
    import urllib.request
    rows = [r for i in range(9) for r in _draft(f"v{i}", 20, 6, 5)]         # 9 篇 × 50 行 = 450 行
    sent: list = []

    class Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=60):
        if len(sent) == 1:
            sent.append(None)
            raise OSError("connection reset")           # 第 2 批炸
        sent.append(json.loads(req.data))
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(C.LedgerWriteError) as ei:
        C.postgrest_upsert(rows, "http://h", "k", batch=120)
    e = ei.value
    assert (e.written, e.total, e.failed_batch, e.uncertain) == (100, 450, 2, 100)
    assert len(sent) == 2, "炸了就停"
    landed = {r["subject_id"] for r in sent[0]}
    want = {r["subject_id"] for r in rows if _family(r) in ("v0", "v1")}
    assert landed == want, "已确认写入的那一批是两篇完整的稿子 (含全部段落行)"
    per_subject = {}
    for r in sent[0]:
        per_subject[r["subject_id"]] = per_subject.get(r["subject_id"], 0) + 1
    full = {}
    for r in rows:
        full[r["subject_id"]] = full.get(r["subject_id"], 0) + 1
    assert all(per_subject[s] == full[s] for s in landed), "落库的 subject 每题都在, 没有只写了一半的"


def test_sql_statements_follow_the_same_subject_boundaries():
    rows = [dict(r, question_version="1", run_tag="shadow-t") for i in range(3) for r in _draft(f"v{i}", 20, 6, 5)]
    stmts = [s for s in C.rows_to_sql(rows, batch=120).split("INSERT INTO")[1:]]
    assert len(stmts) == 2
    assert "'v2'" not in stmts[0] and "'v2:p1'" not in stmts[0] and "'v2'" in stmts[1] and "'v0'" not in stmts[1]
