import copy
import json
from pathlib import Path

import pytest

from judge.banks import build_questions, load_bank
from judge.comments import fill_for
from scripts.backfill_comments import post_for, state_reader
from scripts import evaluate_comment_holdout as H
from scripts import evaluate_comment_reference as E


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def packet(tmp_path):
    bank = load_bank(H.BANK)
    notes = [{"comment_id": f"c{i}", "note_id": f"new_rec{i}", "project_id": "P",
              "category": "保健品", "comment_role": "素人", "title": f"合成标题{i}",
              "raw_content": f"【正文】第{i}条新的合成内容，只有测试用的细节。",
              "comment_text": f"第{i}条合成新评论"} for i in range(40)]
    sample, dev, questions = [tmp_path / name for name in ("sample.json", "dev.json", "questions.json")]
    sample_doc = {"sample_version": "synthetic", "notes": notes, "used_for_prompt_design": False,
                  "reference_labels": None, "model_calls": 0}
    dump(sample, sample_doc)
    dump(dev, {"notes": [{**notes[0], "comment_id": f"old{i}", "note_id": f"old_rec{i}",
                         "comment_text": f"开发合成评论{i}"} for i in range(100)]})
    rendered = [{"comment_id": row["comment_id"], "state": state_reader(row),
                 "fill": fill_for(post_for(row)), "questions": build_questions(bank, fill=fill_for(post_for(row)))}
                for row in notes]
    dump(questions, rendered)
    workbook = tmp_path / "returned.xlsx"
    workbook.write_bytes(b"synthetic source workbook bytes: never real comments")
    reference, manifest = tmp_path / "reference.json", tmp_path / "manifest.json"
    cases = []
    for row in rendered:
        labels = {q.id: "否" if q.jtype == "noul" else next(iter(q.criteria)) for q in bank.questions}
        cases.append({"id": row["comment_id"], "state": row["state"], "fill": row["fill"], "expect": labels,
                      "provenance": {q: {"kind": "returned_reviewer_label", "reviewer": "synthetic"} for q in bank.ids()}})
    cases[0]["expect"]["speech_act"] = "亲历背书"
    cases[1]["expect"]["arranged"] = "是"
    doc = {"reference_version": "synthetic-holdout", "sample_version": "synthetic", "sample_sha256": E.sha(sample),
           "development_sample_sha256": E.sha(dev), "bank_version": bank.version, "bank_sha256": bank.sha256,
           "filled_questions_sha256": E.sha(questions), "review_workbook_sha256": E.sha(workbook), "model": bank.model,
           "reference_scope": H.SCOPE, "reference_ready": True, "label_decisions_final": True,
           "accepted": False, "release_accepted": False, "human_gold_ready": False,
           "model_calls_before_reference_freeze": 0,
           "reference_provenance": {"kind": "single_reviewer_returned_labels", "reviewer": "synthetic",
                                    "reviewer_count": 1, "reviewer_label_cells": 280, "AI_label_overrides": 0,
                                    "definition_conformance_accepted": False}, "cases": cases}
    dump(reference, doc)
    meta = {k: doc[k] for k in H.BOUND}
    meta.update(reference_sha256=E.sha(reference), labels_frozen=True, count=40, cells=280)
    dump(manifest, meta)
    return {"reference": reference, "manifest": manifest, "questions": questions, "sample": sample,
            "development_sample": dev, "review_workbook": workbook, "output": tmp_path / "out"}


def reseal(p, doc):
    dump(p["reference"], doc)
    meta = json.loads(p["manifest"].read_text())
    meta.update({k: doc[k] for k in H.BOUND}, reference_sha256=E.sha(p["reference"]))
    dump(p["manifest"], meta)


def receipt(p):
    binding = H.load_reference(**{k: v for k, v in p.items() if k != "output"})[2]
    path = p["output"].parent / "authorization.json"
    dump(path, {"approved": True, "scope": "one_independent_holdout_candidate3_batch",
                "binding": binding, "max_cases": 40, "owner_instruction": "synthetic fixture permission only",
                "output_directory": str(p["output"].resolve())})
    return path


class Client:
    mock = False  # synthetic test fixture; never used with real data

    def __init__(self, p, fail=None):
        self.rows = {c["state"]["评论原文"]: c for c in json.loads(p["reference"].read_text())["cases"]}
        self.calls = []
        self.fail = fail

    def call(self, body):
        row = self.rows[body["state"]["评论原文"]]
        self.calls.append(row["id"])
        if row["id"] == self.fail:
            raise RuntimeError("private text and private credential must never reach the report")
        return {"answers": {qid: {"noul": .99 if label == "是" else .01}
                            if question["type"] == "noul" else {"choice": label, "probabilities": {
                                option: .99 if option == label else .001 for option in question["criteria"]}}
                            for qid, question in body["questions"].items() for label in [row["expect"][qid]]}}


def test_validate_only_and_authorization_guard_make_no_client(packet, monkeypatch):
    monkeypatch.setattr(H, "JevClient", lambda: pytest.fail("must not create a model client"))
    assert H.evaluate(**packet, validate_only=True) == 0
    assert json.loads((packet["output"] / "status.json").read_text())["model_calls"] == 0
    other = {**packet, "output": packet["output"].parent / "blocked"}
    assert H.evaluate(**other) == 5


def test_40_cases_280_denominator_and_safe_failure(packet):
    client = Client(packet, fail="c2")
    assert H.evaluate(**packet, client=client, authorization=receipt(packet)) == 1
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["count"] == 40 and summary["cells"] == 280
    assert summary["answer_agreement_cells"] == 273 and summary["invalid_or_missing_cells"] == 7
    assert all(x["total"] == 40 for x in summary["per_question"].values())
    assert not summary["release_accepted"] and not summary["human_gold_ready"]
    for file in packet["output"].iterdir():
        assert "private credential" not in file.read_text() and "合成新评论" not in file.read_text()


def test_live_resume_reuses_complete_cases_without_authorization_or_credentials(packet, monkeypatch):
    assert H.evaluate(**packet, client=Client(packet), authorization=receipt(packet)) == 0
    monkeypatch.setattr(H, "JevClient", lambda: pytest.fail("complete checkpoint needs no model client"))
    args = {**packet, "output": packet["output"].parent / "recovered"}
    assert H.evaluate(**args, resume=packet["output"] / "checkpoint.json") == 0
    assert json.loads((args["output"] / "summary.json").read_text())["model_calls_this_run"] == 0


@pytest.mark.parametrize("kind", ["count", "label", "source", "context", "bank", "state", "sample_digest"])
def test_invalid_inputs_rejected_before_client(packet, kind, monkeypatch):
    doc = json.loads(packet["reference"].read_text())
    if kind == "count": doc["cases"].pop()
    if kind == "label": doc["cases"][0]["expect"]["detail_level"] = "invented"
    if kind == "source": doc["cases"][0]["provenance"]["arranged"]["reviewer"] = "another"
    if kind == "context": doc["cases"][0]["state"]["帖子正文"] = "changed"
    if kind == "bank": doc["bank_sha256"] = "0" * 64
    if kind == "state": doc["release_accepted"] = True
    if kind == "sample_digest": doc["sample_sha256"] = "0" * 64
    reseal(packet, doc)
    monkeypatch.setattr(H, "JevClient", lambda: pytest.fail("invalid input must not call a model"))
    with pytest.raises(ValueError): H.evaluate(**packet)


@pytest.mark.parametrize("kind", ["post", "text"])
def test_development_overlap_rejected_even_with_recomputed_digest(packet, kind):
    sample = json.loads(packet["sample"].read_text())
    dev = json.loads(packet["development_sample"].read_text())
    if kind == "post": dev["notes"][0]["note_id"] = sample["notes"][0]["note_id"]
    else: dev["notes"][0]["comment_text"] = sample["notes"][0]["comment_text"]
    dump(packet["development_sample"], dev)
    doc = json.loads(packet["reference"].read_text())
    doc["development_sample_sha256"] = E.sha(packet["development_sample"])
    reseal(packet, doc)
    with pytest.raises(ValueError, match="overlaps"): H.evaluate(**packet)


def test_cross_bank_resume_and_nonempty_or_git_output_rejected(packet, tmp_path):
    assert H.evaluate(**packet, client=Client(packet), authorization=receipt(packet)) == 0
    cp = json.loads((packet["output"] / "checkpoint.json").read_text())
    cp["binding"]["bank_sha256"] = "another"
    path = tmp_path / "wrong-checkpoint.json"
    dump(path, cp)
    with pytest.raises(ValueError, match="mismatch"):
        H.evaluate(**{**packet, "output": tmp_path / "new"}, resume=path)
    with pytest.raises(ValueError, match="empty"): H.evaluate(**packet)
    repo = tmp_path / "actual-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git/HEAD").write_text("ref: refs/heads/main")
    with pytest.raises(ValueError, match="outside Git"):
        H.evaluate(**{**packet, "output": repo / "out"})


def test_authorization_bound_to_exact_reference(packet):
    auth = receipt(packet)
    value = json.loads(auth.read_text()); value["binding"]["reference_sha256"] = "another"
    dump(auth, value)
    with pytest.raises(ValueError, match="authorization"):
        H.evaluate(**packet, client=Client(packet), authorization=auth)


def test_authorization_cannot_start_another_output_batch(packet, tmp_path):
    auth = receipt(packet)
    with pytest.raises(ValueError, match="authorization"):
        H.evaluate(**{**packet, "output": tmp_path / "another-batch"}, client=Client(packet), authorization=auth)


def test_false_positives_and_false_negatives_both_reported(packet):
    client = Client(packet)
    client.rows[next(iter(client.rows))]["expect"]["speech_act"] = "回复答疑"
    client.rows[list(client.rows)[1]]["expect"]["speech_act"] = "旁观推荐"
    assert H.evaluate(**packet, client=client, authorization=receipt(packet)) == 3
    risks = json.loads((packet["output"] / "summary.json").read_text())["reader_bank_risk_signals"]
    assert risks["endorsement_unambiguous_false_positive"] == ["c1"]
    assert risks["endorsement_false_negative_all_valid"] == ["c0"]


def test_old_validator_still_rejects_40_case_reference(packet):
    with pytest.raises(ValueError):
        E.load_reference(packet["reference"], packet["manifest"], packet["questions"])
