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
