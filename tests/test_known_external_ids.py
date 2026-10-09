# -*- coding: utf-8 -*-
"""known_ids 只收账本写齐了的外部笔记 (codex review on #6, P1)。

apply_rows 先写 external_notes 再写账本; 账本那步失败 → job 红、cache 不存, 但 external_notes 已经有这批。
known_ids 若照 external_notes 全收, 下周这批按 known 跳过、永远不再判: 账本那一半永久丢。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.known_external_ids as K  # noqa: E402


def test_complete_ids_requires_full_ledger():
    counts = Counter({"a": 20, "b": 3, "c": 0})
    done, short = K.complete_ids(["a", "b", "c", "d"], counts, need=20)
    assert done == ["a"] and short == ["b", "c", "d"], "半写 (b) / 没写 (c, d) 的都不算 known, 下周重判"


def test_main_writes_only_complete_notes(monkeypatch, tmp_path):
    def fake_get(url, key, path):
        if path.startswith("external_notes?"):
            return [] if "offset=1000" in path else [{"note_id": "n1"}, {"note_id": "n2"}, {"note_id": "n3"}]
        if path.startswith("note_feature_answers?"):
            assert "subject_type=eq.external_note" in path and "order=subject_id,question_id,question_version,extractor,run_tag" in path
            return [] if "offset=1000" in path else [{"subject_id": "n1"}] * 20 + [{"subject_id": "n2"}] * 5
        raise AssertionError(path)
    monkeypatch.setattr(K, "_get", fake_get)
    monkeypatch.setattr(K, "expected_rows", lambda: 20)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    out = tmp_path / "known.txt"
    assert K.main([str(out)]) == 0
    assert out.read_text(encoding="utf-8").split() == ["n1"], "n2 只写了 5/20 行、n3 一行没有: 都不算 known"


def test_ledger_unreadable_falls_back_to_all_notes_with_a_warning(monkeypatch, tmp_path, capsys):
    def fake_get(url, key, path):
        if path.startswith("external_notes?"):
            return [] if "offset=1000" in path else [{"note_id": "n1"}, {"note_id": "n2"}]
        raise OSError("relation note_feature_answers does not exist")
    monkeypatch.setattr(K, "_get", fake_get)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    out = tmp_path / "known.txt"
    assert K.main([str(out)]) == 0
    assert out.read_text(encoding="utf-8").split() == ["n1", "n2"], "账本读不到: 退回只看 external_notes (这周写库也会红, 修好后下次按账本判)"
    assert "::warning::" in capsys.readouterr().out


def test_expected_rows_is_the_fq_bank_size():
    assert K.expected_rows() == 20
