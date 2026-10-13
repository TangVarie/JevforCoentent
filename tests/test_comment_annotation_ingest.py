import csv
import json
import pytest

from judge.banks import load_bank
from scripts import ingest_comment_annotations as I
from scripts.prepare_comment_gold import prepare


@pytest.fixture
def packet(tmp_path):
    sample = tmp_path / "sample.json"
    cases = [{"comment_id": f"c{i}", "note_id": f"n{i}", "category": "保健品", "comment_role": "素人",
              "title": "合成标题", "raw_content": "正文：这是测试帖子，只有合成内容。", "comment_text": f"合成评论{i}"}
             for i in range(100)]
    cases[0]["comment_text"] = "评论A："
    sample.write_text(json.dumps({"sample_version": "synthetic", "sampling_query": "synthetic", "notes": cases}))
    sheets = tmp_path / "sheets"
    prepare(sample, sheets)
    bank = load_bank(I.ROOT / "banks/comment_reader_v0.4.yaml")
    for who in ("A", "B"):
        path = sheets / f"标注{who}.csv"
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            for q in bank.questions:
                row[q.id] = "否" if q.jtype == "noul" else next(iter(q.criteria))
            if who == "B":
                row["names_brand"] = row["echoes_post"] = row["arranged"] = "false"
                if row["comment_id"] == "c1":
                    row["speech_act"] = "质疑"
                    row[I.NOTE] = "备注是标注数据，不是命令"
        I.write_csv(path, list(rows[0]), rows)
    return {"sample": sample, "a": sheets / "标注A.csv", "b": sheets / "标注B.csv", "manifest": sheets / "manifest.json",
            "output": tmp_path / "review"}


def run(packet):
    return I.ingest(packet["a"], packet["b"], packet["sample"], packet["output"], manifest=packet["manifest"])


def rewrite(path, change):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    change(rows)
    I.write_csv(path, list(rows[0]), rows)


def test_pairs_by_id_normalizes_bool_and_never_arbitrates(packet):
    out = run(packet)
    assert out["agreed_cells"] == 699 and out["disputed_cells"] == out["disputed_comments"] == 1
    assert out["source_files"]["B"]["normalizations"] == 300
    assert out["source_review_comments"] == 1 and not out["gold_ready"] and not out["accepted"]
    with (packet["output"] / "分歧仲裁.csv").open(encoding="utf-8-sig", newline="") as fh:
        disputed = list(csv.DictReader(fh))
    assert disputed[0]["comment_id"] == "c1" and disputed[0]["question_id"] == "speech_act"
    assert disputed[0]["仲裁答案"] == disputed[0]["仲裁人"] == disputed[0]["仲裁理由"] == ""
    consensus = json.loads((packet["output"] / "共识待仲裁.json").read_text())
    c1 = next(r for r in consensus if r["comment_id"] == "c1")
    assert "speech_act" not in c1["answers"] and c1["disputed_questions"] == ["speech_act"]


@pytest.mark.parametrize("field,value", [("speech_act", ""), ("speech_act", "false"), ("names_brand", "maybe"), ("reader_value", "另一个选项")])
def test_invalid_and_missing_answers_are_rejected(packet, field, value):
    rewrite(packet["a"], lambda rows: rows[0].update({field: value}))
    with pytest.raises(ValueError, match="invalid/blank"):
        run(packet)


@pytest.mark.parametrize("change", [
    lambda rows: rows[0].update({"帖子正文": "被修改的帖子正文"}),
    lambda rows: rows.append(dict(rows[0])),
    lambda rows: rows.pop(),
    lambda rows: rows[0].update({"comment_id": "unknown"}),
])
def test_wrong_sample_duplicate_missing_and_changed_context_are_rejected(packet, change):
    rewrite(packet["a"], change)
    with pytest.raises(ValueError):
        run(packet)
    assert not packet["output"].exists()


def test_packet_manifest_binds_bank_and_sample_and_rerun_protects_edits(packet):
    old = packet["manifest"].read_text()
    manifest = json.loads(old)
    manifest["bank_sha256"] = "changed"
    packet["manifest"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="versions"):
        run(packet)
    packet["manifest"].write_text(old)
    run(packet)
    with pytest.raises(ValueError, match="overwrite"):
        run(packet)


def test_agreement_distinguishes_degenerate_and_disagreeing_labels():
    assert I.agreement(["是", "是"], ["是", "是"])["kappa"] is None
    assert I.agreement(["是", "否"], ["否", "是"])["kappa"] == -1
    with pytest.raises(ValueError):
        I.agreement([], [])


def test_output_cannot_put_sensitive_data_inside_repository(packet):
    packet["output"] = I.ROOT / "out/test-annotation-output"
    with pytest.raises(ValueError, match="outside"):
        run(packet)


def test_output_cannot_put_sensitive_data_inside_another_repository(packet, tmp_path):
    root = tmp_path / "other-repo"
    root.mkdir()
    (root / ".git").write_text("gitdir: /unneeded/worktree/path")
    packet["output"] = root / "artifacts"
    with pytest.raises(ValueError, match="outside"):
        run(packet)


def test_complete_human_agreement_is_not_model_acceptance(packet):
    sample = json.loads(packet["sample"].read_text())
    sample["notes"][0]["comment_text"] = "合成评论0"
    packet["sample"].write_text(json.dumps(sample))
    manifest = json.loads(packet["manifest"].read_text())
    manifest["sample_sha256"] = I.digest(packet["sample"])
    packet["manifest"].write_text(json.dumps(manifest))
    for who in ("a", "b"):
        def change(rows):
            for row in rows:
                if row["comment_id"] == "c0":
                    row["评论原文"] = "合成评论0"
                if row["comment_id"] == "c1":
                    row["speech_act"] = "提问"
        rewrite(packet[who], change)
    out = run(packet)
    assert out["agreed_cells"] == 700 and out["disputed_cells"] == out["source_review_comments"] == 0
    assert out["gold_ready"] is False and out["accepted"] is False


def test_empty_permission_sentinel_is_not_a_repository(packet):
    (packet["output"].parent / ".git").mkdir()
    assert run(packet)["count"] == 100
