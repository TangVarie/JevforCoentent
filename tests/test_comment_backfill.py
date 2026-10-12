import csv
import json
import sys

import pytest

from judge.banks import load_bank
from judge.core import judge_state, ledger_rows
from scripts import backfill_comments as B
from scripts.prepare_comment_gold import prepare
from tests.test_comment_api import Answers


def test_health_annotation_packet_is_blank_and_independent(tmp_path):
    # Real source texts stay local; public CI validates shape using synthetic health comments.
    sample = tmp_path / "synthetic-sample.json"
    comments = [{"comment_id": f"c{i}", "note_id": f"n{i}", "comment_text": f"合成提问{i}怎么买",
                 "comment_role": "素人", "category": "OTC药" if i < 35 else ("保健品" if i < 70 else "处方药"),
                 "title": "合成帖子", "raw_content": "正文：用于测试的帖子，不是真实原文。"} for i in range(100)]
    sample.write_text(json.dumps({"notes": comments, "sample_version": "synthetic", "sampling_query": "synthetic"}))
    manifest = prepare(sample, tmp_path)
    sheets = []
    for who in ("A", "B"):
        with (tmp_path / f"标注{who}.csv").open(encoding="utf-8-sig") as fh:
            sheets.append(list(csv.DictReader(fh)))
    bank = load_bank(B.READER)
    assert manifest["categories"] == {"OTC药": 35, "保健品": 35, "处方药": 30}
    assert len(sheets[0]) == len(sheets[1]) == 100
    assert {r["comment_id"] for r in sheets[0]} == {r["comment_id"] for r in sheets[1]}
    assert [r["comment_id"] for r in sheets[0]] != [r["comment_id"] for r in sheets[1]]
    assert all(not row[q] for sheet in sheets for row in sheet for q in bank.by_id())
    assert not any("互动" in key or "tier" in key for key in sheets[0][0])


def test_backfill_context_does_not_assume_every_comment_is_operations():
    c = {"comment_text": "怎么买", "comment_role": "素人", "note_id": "LNKT_01",
         "raw_content": "标题：戒烟第三天\n正文：嘴里没味，靠嗑瓜子撑着。"}
    st = B.state_ops(c)
    assert st["评论角色"] == "素人" and "运营在它下面写" not in st["说明"]
    assert B.post_for(c)["kind"] == "product"
    assert "商家" == B.fill_for(B.post_for(c))["arranger"]


def test_resume_retries_partial_invalid_wrong_digest_and_shadow_tags():
    bank = load_bank(B.READER)
    result = judge_state(Answers(), bank, "c1", {"评论原文": "怎么买"}, fill=B.fill_for({"body": "正文"}))
    rows = ledger_rows(result, bank, run_tag="shadow-test")
    assert B.complete(rows, "c1", bank, "shadow-test")
    assert not B.complete(rows[:-1], "c1", bank, "shadow-test")
    assert not B.complete(rows, "c1", bank, "shadow-other")
    assert not B.complete([{**r, "bank_sha256": "wrong"} for r in rows], "c1", bank, "shadow-test")
    assert not B.complete([{**r, "invalid_reason": "missing", "answer": None} for r in rows], "c1", bank, "shadow-test")


def test_checkpoint_can_resume_without_model_calls_and_keeps_summary(monkeypatch, tmp_path):
    c = {"comment_id": "c1", "note_id": "n1", "comment_text": "怎么买呢", "raw_content": "正文够长"}
    source, rows_path, summary = tmp_path / "input.json", tmp_path / "rows.json", tmp_path / "summary.csv"
    source.write_text(json.dumps([c]), encoding="utf-8")
    fake = Answers()
    monkeypatch.setattr(B, "JevClient", lambda **kw: fake)
    cmd = ["backfill_comments", "--input", str(source), "--rows-json", str(rows_path), "--summary", str(summary)]
    monkeypatch.setattr(sys, "argv", cmd)
    B.main()
    assert len(fake.calls) == 2
    fake.fail = True
    monkeypatch.setattr(sys, "argv", cmd + ["--resume", str(rows_path)])
    B.main()
    assert len(fake.calls) == 2
    assert len(json.loads(rows_path.read_text())) == 9
    assert "提问" in summary.read_text() and "n1,1" in summary.read_text()


def test_failure_keeps_other_bank_rows_and_exits_failure(monkeypatch, tmp_path):
    source, rows = tmp_path / "input.json", tmp_path / "rows.json"
    source.write_text(json.dumps([{"comment_id": "c1", "note_id": "n1", "comment_text": "怎么买"}]))
    fake = Answers()
    fake.omit = {"arranged"}
    monkeypatch.setattr(B, "JevClient", lambda **kw: fake)
    monkeypatch.setattr(sys, "argv", ["backfill_comments", "--input", str(source), "--rows-json", str(rows)])
    with pytest.raises(SystemExit) as exc:
        B.main()
    assert exc.value.code == 1 and len(json.loads(rows.read_text())) == 9


def test_primary_and_mock_write_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(B, "fetch", lambda *a: pytest.fail("未经人工验收先读库了"))
    for extra in (["--run-tag", "primary"], ["--mock"]):
        monkeypatch.setattr(sys, "argv", ["backfill_comments", "--from-db", "--write", *extra])
        with pytest.raises(SystemExit) as exc:
            B.main()
        assert exc.value.code == 2


def test_acceptance_requires_two_people_and_current_bank_digest(tmp_path):
    bank = load_bank(B.READER)
    path = tmp_path / "acceptance.json"
    record = {"accepted": True, "sample_count": 100, "independent_annotators": ["A", "A"],
              "arbitration_artifact": "gold.json", "evaluation_artifact": "metrics.json", "accepted_by": "owner",
              "bank_sha256": {bank.name: bank.sha256}}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        B.validate_acceptance(str(path), [(bank, B.state_reader)])
    record["independent_annotators"] = ["A", "B"]
    record["bank_sha256"][bank.name] = "old"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="摘要"):
        B.validate_acceptance(str(path), [(bank, B.state_reader)])
