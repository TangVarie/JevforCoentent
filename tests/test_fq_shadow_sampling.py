# -*- coding: utf-8 -*-
"""fq_shadow --from-db 要能产出 docs/28 §6.1 的闸一样本: 5 项目 × (30 爆 + 30 趴), 固定种子, 名单可存档 (TV 审计 C-10)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.fq_shadow as F  # noqa: E402


def _labels():
    rows = []
    for pid, n_pos, n_neg in [("NUC_phase1", 85, 300), ("NRT_phase2", 42, 120), ("SPX_phase1", 12, 50)]:
        rows += [{"note_id": f"{pid}_p{i}", "project_id": pid, "y": 1} for i in range(n_pos)]
        rows += [{"note_id": f"{pid}_n{i}", "project_id": pid, "y": "0"} for i in range(n_neg)]   # y 可能是串
    return rows


def test_gate1_sample_is_30_plus_30_per_project_and_takes_all_when_short():
    s = F.gate1_sample(_labels(), per_class=30, seed=1)
    by = {}
    for r in s:
        by.setdefault(r["project_id"], {0: 0, 1: 0})[r["y"]] += 1
    assert by["NUC_phase1"] == {1: 30, 0: 30} and by["NRT_phase2"] == {1: 30, 0: 30}
    assert by["SPX_phase1"] == {1: 12, 0: 30}, "正例不足 30 全取, 负例照抽 30"
    assert len({r["note_id"] for r in s}) == len(s), "名单里没有重复"


def test_gate1_sample_is_deterministic_and_order_independent():
    a = F.gate1_sample(_labels(), per_class=30, seed=7)
    b = F.gate1_sample(list(reversed(_labels())), per_class=30, seed=7)
    assert a == b, "库返回顺序变了, 同一个 seed 仍是同一份名单 (名单要能存档复现)"
    c = F.gate1_sample(_labels(), per_class=30, seed=8)
    assert a != c, "换 seed 要换名单, 否则 seed 是摆设"


def test_fetch_from_db_gate1_pages_labels_and_chunks_ids(monkeypatch, tmp_path):
    calls: list[str] = []
    labels = _labels()
    notes = {r["note_id"]: {"note_id": r["note_id"], "project_id": r["project_id"], "title": "t", "raw_content": "x"} for r in labels}

    def fake_pg(url, key, path):
        calls.append(path)
        q = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&") if "=" in kv)
        limit, offset = int(q.get("limit", 1000)), int(q.get("offset", 0))
        if path.startswith("v_l2_labels?"):
            rows = [r for r in labels if r["project_id"] in q["project_id"][4:-1].split(",")]
            return rows[offset: offset + min(limit, 1000)]
        if path.startswith("notes?"):
            ids = q["note_id"][4:-1].split(",")
            return [notes[i] for i in ids][offset: offset + limit]
        return []                                                           # 账本: 没有现行答案也行
    monkeypatch.setattr(F, "_pg", fake_pg)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    out = tmp_path / "sample.json"
    got, opus = F.fetch_from_db(0, None, {}, gate1=True, projects=("NUC_phase1", "NRT_phase2", "SPX_phase1"),
                                per_class=30, seed=3, sample_out=str(out))
    assert len(got) == 30 + 30 + 30 + 30 + 12 + 30, f"实际 {len(got)}"
    arch = json.loads(out.read_text(encoding="utf-8"))
    assert arch["seed"] == 3 and arch["n"] == len(got) and {s["note_id"] for s in arch["sample"]} == {n["note_id"] for n in got}, "名单存档要和真取的篇一致"
    label_pages = [p for p in calls if p.startswith("v_l2_labels?")]
    assert len(label_pages) >= 1 and all("limit=" in p and "offset=" in p for p in label_pages), "v_l2_labels 要翻页 (609 行 > 一页也不能钳)"
    note_calls = [p for p in calls if p.startswith("notes?")]
    assert all(len(p.split("note_id=in.(")[1].rstrip(")").split(",")) <= 150 for p in note_calls), "按 150 个一组查, URL 别爆"
    ans_calls = [p for p in calls if p.startswith("note_feature_answers?")]
    assert ans_calls and all("order=subject_type,subject_id,question_id,question_version,extractor" in p for p in ans_calls), \
        "账本翻页要按主键全序排 (run_tag 已固定), 否则同一时刻的不同 question_version / extractor 行在页边界会重复或漏"
