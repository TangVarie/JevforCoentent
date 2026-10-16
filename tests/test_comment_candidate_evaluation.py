import json
from pathlib import Path

import pytest

from judge.banks import discover
from scripts import evaluate_comment_candidate as C
from scripts import evaluate_comment_reference as E
from scripts.backfill_comments import post_for, state_reader
from judge.comments import fill_for


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def packet(tmp_path, monkeypatch):
    fixture = json.loads((E.ROOT / "fixtures/comment_candidate4_spec.json").read_text())
    bank = C.candidate()
    trusted = tmp_path / "trusted-contract.json"
    contract = json.loads(C.CONTRACT.read_text())
    # Test-only trust anchor for invented data. Production contract remains 20/40/40.
    contract["scope_counts"] = {scope: 20 for scope in C.SCOPES}
    save(trusted, contract)
    monkeypatch.setattr(C, "CONTRACT", trusted)
    rows = fixture["notes"]
    labels = fixture["expected_labels"]
    sample = tmp_path / "sample.json"
    save(sample, {**fixture, "notes": rows})
    reference, protocol, freeze = [tmp_path / n for n in ("reference.json", "protocol.json", "design.json")]
    thresholds = {"overall_agreement_min": .85, "per_question_agreement_min": .75,
                  "ambiguous_fraction_max": .25, "unambiguous_endorsement_false_negatives_max": 0}
    save(freeze, {"bank_sha256": bank.sha256, "candidate": bank.version, "model_diagnostic_gate": thresholds})
    doc = {"scope": "synthetic_specification", "bank_sha256": bank.sha256,
           "reference_ready": True, "model_calls_before_freeze": 0,
           "source_kind": "AI_definition_reference", "reference_author": "synthetic AI",
           "reference_provenance": {"kind": "AI_definition_reference", "cells": 140, "independent_human_gold": False},
           "accepted": False, "release_accepted": False, "human_gold_ready": False,
           "cases": [{"id": row["comment_id"], "state": state_reader(row), "fill": fill_for(post_for(row)),
                      "expect": labels[i]} for i, row in enumerate(rows)]}
    save(reference, doc)
    save(protocol, {"scope": doc["scope"], "count": 20, "cells": 140, "sample_sha256": E.sha(sample),
         "reference_sha256": E.sha(reference), "bank_sha256": bank.sha256, "bank_version": bank.version,
         "model": bank.model, "design_freeze": {"path": str(freeze), "sha256": E.sha(freeze)},
         "diagnostic_thresholds": thresholds})
    return dict(sample=sample, reference=reference, protocol=protocol, output=tmp_path / "out")


def receipt(packet):
    binding = C.read_packet(**{k: v for k, v in packet.items() if k != "output"})[3]
    path = packet["output"].parent / "authorization.json"
    save(path, {"approved": True, "binding": binding, "output_directory": str(packet["output"].resolve()),
                "max_cases": binding["count"], "owner_instruction": "synthetic test authorization only"})
    return path


class Client:
    mock = False  # fixture; never used outside synthetic tests

    def __init__(self, packet, fail=None):
        self.cases = {c["state"]["评论原文"]: c for c in json.loads(packet["reference"].read_text())["cases"]}
        self.calls = []
        self.fail = fail

    def call(self, body):
        case = self.cases[body["state"]["评论原文"]]
        self.calls.append(case["id"])
        if case["id"] == self.fail:
            raise RuntimeError("private original and credential must not be echoed")
        return {"answers": {qid: {"noul": .99 if case["expect"][qid] == "是" else .01}
                if q["type"] == "noul" else {"choice": case["expect"][qid], "probabilities": {
                    label: .99 if label == case["expect"][qid] else .001 for label in q["criteria"]}}
                for qid, q in body["questions"].items()}}


def test_diagnostic_pass_never_grants_release_or_human_gold(packet, monkeypatch):
    client = Client(packet)
    assert C.evaluate(**packet, authorization=receipt(packet), client=client) == 0
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["cells"] == summary["answer_agreement_cells"] == 140
    assert summary["diagnostic_gate"]["passed"]
    assert not summary["release_accepted"] and not summary["human_gold_ready"]
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("complete resume must not make a client"))
    args = {**packet, "output": packet["output"].parent / "recovered"}
    assert C.evaluate(**args, resume=packet["output"] / "checkpoint.json") == 0
    assert json.loads((args["output"] / "summary.json").read_text())["model_calls_this_run"] == 0


def test_failure_keeps_fixed_denominator_and_no_private_exception(packet):
    client = Client(packet, fail="spec-02")
    assert C.evaluate(**packet, authorization=receipt(packet), client=client) == 1
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["cells"] == 140 and summary["invalid_or_missing_cells"] == 7
    assert "invalid_or_missing" in summary["diagnostic_gate"]["reasons"]
    assert all("private original" not in p.read_text() for p in packet["output"].glob("*.json"))


def test_preflight_and_no_authorization_do_not_construct_client(packet, monkeypatch):
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("unexpected client"))
    assert C.evaluate(**packet, validate_only=True) == 0
    args = {**packet, "output": packet["output"].parent / "blocked"}
    assert C.evaluate(**args) == 5


@pytest.mark.parametrize("kind", ["state", "closed_label", "provenance", "release", "bank", "count"])
def test_resealed_bad_inputs_are_still_rejected_before_calls(packet, kind, monkeypatch):
    doc = json.loads(packet["reference"].read_text())
    plan = json.loads(packet["protocol"].read_text())
    if kind == "state": doc["cases"][0]["state"]["评论原文"] = "changed"
    if kind == "closed_label": doc["cases"][0]["expect"]["arranged"] = "unknown"
    if kind == "provenance": doc["reference_provenance"]["independent_human_gold"] = True
    if kind == "release": doc["release_accepted"] = True
    if kind == "bank": doc["bank_sha256"] = "bad"
    if kind == "count": plan["count"] = 21; plan["cells"] = 147
    save(packet["reference"], doc)
    plan["reference_sha256"] = E.sha(packet["reference"])
    save(packet["protocol"], plan)
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("invalid packet should not call"))
    with pytest.raises(ValueError): C.evaluate(**packet)


def test_unseen_overlap_is_rejected_even_if_source_digests_match(packet):
    doc = json.loads(packet["reference"].read_text())
    doc["scope"] = "unseen_AI_definition_reference"
    save(packet["reference"], doc)
    plan = json.loads(packet["protocol"].read_text())
    plan.update(scope=doc["scope"], reference_sha256=E.sha(packet["reference"]),
                excluded_samples=[{"path": str(packet["sample"]), "sha256": E.sha(packet["sample"])}])
    save(packet["protocol"], plan)
    with pytest.raises(ValueError, match="overlaps"): C.evaluate(**packet)


def test_returned_reference_cannot_be_changed_under_development_scope(packet):
    original = json.loads(packet["reference"].read_text())
    source = packet["output"].parent / "source.json"
    save(source, original)
    contract = json.loads(C.CONTRACT.read_text())
    contract["returned_reference_sha256"] = E.sha(source)
    save(C.CONTRACT, contract)
    original.update(scope="development_single_reviewer", source_kind="returned_reviewer_label")
    original["reference_provenance"]["kind"] = "returned_reviewer_label"
    original["cases"][0]["expect"]["names_brand"] = "否"
    save(packet["reference"], original)
    plan = json.loads(packet["protocol"].read_text())
    plan.update(scope=original["scope"], reference_sha256=E.sha(packet["reference"]), source_reference_sha256=E.sha(source))
    save(packet["protocol"], plan)
    with pytest.raises(ValueError, match="must not be overwritten"):
        C.evaluate(**packet, source_reference=source)


def test_thresholds_cannot_be_relaxed_after_preregistration(packet):
    plan = json.loads(packet["protocol"].read_text())
    plan["diagnostic_thresholds"]["overall_agreement_min"] = 0
    save(packet["protocol"], plan)
    with pytest.raises(ValueError, match="pre-registered"):
        C.evaluate(**packet)


def test_source_and_protocol_cannot_be_resealed_together_to_change_human_labels(packet, monkeypatch):
    doc = json.loads(packet["reference"].read_text())
    source = packet["output"].parent / "source-original.json"
    save(source, doc)
    contract = json.loads(C.CONTRACT.read_text())
    contract["returned_reference_sha256"] = E.sha(source)
    save(C.CONTRACT, contract)
    doc["cases"][0]["expect"]["names_brand"] = "否"
    save(source, doc)
    doc.update(scope="development_single_reviewer", source_kind="returned_reviewer_label")
    doc["reference_provenance"]["kind"] = "returned_reviewer_label"
    save(packet["reference"], doc)
    plan = json.loads(packet["protocol"].read_text())
    plan.update(scope=doc["scope"], reference_sha256=E.sha(packet["reference"]),
                source_reference_sha256=E.sha(source))
    save(packet["protocol"], plan)
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("untrusted source must not reach model"))
    with pytest.raises(ValueError, match="unchanged source"):
        C.evaluate(**packet, source_reference=source)


@pytest.mark.parametrize("bad", [None, True, "0.85", float("nan"), float("inf"), -0.1, 1.1])
def test_threshold_schema_is_checked_even_when_mutable_design_is_resealed(packet, monkeypatch, bad):
    plan = json.loads(packet["protocol"].read_text())
    freeze_path = Path(plan["design_freeze"]["path"])
    freeze = json.loads(freeze_path.read_text())
    if bad is None:
        plan["diagnostic_thresholds"].pop("overall_agreement_min")
    else:
        plan["diagnostic_thresholds"]["overall_agreement_min"] = bad
    freeze["model_diagnostic_gate"] = plan["diagnostic_thresholds"]
    save(freeze_path, freeze)
    plan["design_freeze"]["sha256"] = E.sha(freeze_path)
    save(packet["protocol"], plan)
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("bad threshold must not consume calls"))
    with pytest.raises(ValueError, match="threshold"):
        C.evaluate(**packet)


def test_truncated_but_resealed_scope_cannot_claim_a_complete_benchmark(packet):
    doc = json.loads(packet["reference"].read_text())
    sample = json.loads(packet["sample"].read_text())
    doc["cases"] = doc["cases"][:1]
    sample["notes"] = sample["notes"][:1]
    save(packet["reference"], doc)
    save(packet["sample"], sample)
    plan = json.loads(packet["protocol"].read_text())
    plan.update(count=1, cells=7, sample_sha256=E.sha(packet["sample"]),
                reference_sha256=E.sha(packet["reference"]))
    save(packet["protocol"], plan)
    with pytest.raises(ValueError, match="count"):
        C.evaluate(**packet)


def test_missing_runner_up_probability_is_invalid_not_unambiguous(packet):
    original = Client(packet)
    class Truncated:
        mock = False
        def call(self, body):
            result = original.call(body)
            if body["state"]["评论原文"] == "宁光LX-12是12W，三档亮度，129元。":
                item = result["answers"]["speech_act"]
                item["probabilities"] = {item["choice"]: .99}
            return result
    assert C.evaluate(**packet, authorization=receipt(packet), client=Truncated()) == 1
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["cells"] == 140 and summary["invalid_or_missing_cells"] == 1
    assert summary["answer_agreement_cells"] == 139
    assert not summary["diagnostic_gate"]["passed"]


def test_v1_checkpoint_is_not_silently_upgraded_to_full_probability_validation(packet, monkeypatch):
    assert C.evaluate(**packet, authorization=receipt(packet), client=Client(packet)) == 0
    path = packet["output"] / "checkpoint.json"
    old = json.loads(path.read_text())
    old["binding"]["evaluator_version"] = "comment-candidate4-diagnostic-v1"
    save(path, old)
    monkeypatch.setattr(C, "JevClient", lambda: pytest.fail("old results cannot trigger a new call"))
    with pytest.raises(ValueError, match="mismatch"):
        C.evaluate(**{**packet, "output": packet["output"].parent / "legacy"}, resume=path)


def test_receipt_cannot_be_reused_for_another_output_batch(packet):
    auth = receipt(packet)
    with pytest.raises(ValueError, match="one batch"):
        C.evaluate(**{**packet, "output": packet["output"].parent / "different"}, authorization=auth)


def test_C4_released_once_byte_identical_and_legacy_contract():
    bank = C.candidate()
    trusted = json.loads((E.ROOT / "config/comment_candidate4_diagnostic_contract.json").read_text())
    assert trusted["scope_counts"] == {"synthetic_specification": 20,
                                        "development_single_reviewer": 40, "unseen_AI_definition_reference": 40}
    assert trusted["returned_reference_sha256"] == "953dc0975d58574045324d55b7f01b97a352b724c12e2c08ee8ff616045341d3"
    # 2026-10-16 owner 验收 D-112：C4 原样发布成 comment_reader_v0.6（只加了 status / frozen_sha256 两行元数据，
    # 规范化 digest 不变）；候选目录那份仍不参与 runtime 发现。
    released = [n for n, p in discover(E.ROOT / "banks").items() if E.load_bank(p).version == bank.version]
    assert released == ["comment_reader_v0.6"]
    assert E.load_bank(discover(E.ROOT / "banks")["comment_reader_v0.6"]).sha256 == bank.sha256
    with pytest.raises(ValueError, match="limited to"):
        E.candidate_for(C.BANK, E.load_bank(E.BANK))
