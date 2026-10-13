import json
from pathlib import Path

import pytest
import yaml

from judge.banks import load_bank
from judge.comments import fill_for
from scripts.backfill_comments import post_for
from scripts.prepare_comment_gold import prepare
from scripts import evaluate_comment_reference as E
from judge.banks import discover

CANDIDATE = E.ROOT / "banks/candidates/comment_reader_v0.5_candidate.yaml"
HISTORICAL_CANDIDATE = E.ROOT / "banks/candidates/comment_reader_v0.5_candidate1.yaml"


@pytest.fixture
def packet(tmp_path):
    sample = tmp_path / "sample.json"
    notes = [{"comment_id": f"c{i}", "note_id": f"n{i}", "category": "保健品", "comment_role": "素人",
              "title": f"合成帖子{i}", "raw_content": f"正文：这是第{i}条合成情境，包含按篇不同的事实和细节。",
              "comment_text": f"合成测试评论{i}"} for i in range(100)]
    sample.write_text(json.dumps({"sample_version": "synthetic", "sampling_query": "synthetic", "notes": notes}))
    original = tmp_path / "packet"
    m = prepare(sample, original)
    rendered = {c["comment_id"]: c for c in json.loads((original / "逐条题面.json").read_text())}
    bank = load_bank(E.BANK)
    cases = []
    for note in notes:
        labels = {q.id: "否" if q.jtype == "noul" else next(iter(q.criteria)) for q in bank.questions}
        cases.append({"id": note["comment_id"], "state": rendered[note["comment_id"]]["state"],
            "fill": fill_for(post_for(note)), "expect": labels,
            "provenance": {qid: {"kind": "AB_agreement"} for qid in labels}})
    cases[0]["expect"]["speech_act"] = "亲历背书"
    cases[0]["provenance"]["speech_act"] = {"kind": "user_adopted_final_arbitration", "declared_arbitrator": "AI"}
    cases[1]["expect"]["arranged"] = "是"
    reference = tmp_path / "reference.json"
    doc = {**{k: m[k] for k in ("sample_version", "sample_sha256", "bank_sha256", "filled_questions_sha256")},
        "reference_version": "synthetic-final", "model": bank.model,
        "reference_ready": True, "label_decisions_final": True, "accepted": False,
        "reference_provenance": {"human_arbitration_claimed": False, "AI_arbitration_cells_adopted_by_user": 1, "AB_agreed_cells": 699},
        "cases": cases}
    reference.write_text(json.dumps(doc, ensure_ascii=False))
    manifest = tmp_path / "manifest.json"
    meta = {k: doc[k] for k in ("reference_version", "sample_version", "sample_sha256", "bank_sha256", "filled_questions_sha256", "model")}
    manifest.write_text(json.dumps({**meta, "reference_sha256": E.sha(reference)}))
    return {"reference": reference, "manifest": manifest, "questions": original / "逐条题面.json",
            "output": tmp_path / "eval", "bank": bank, "doc": doc}


def arguments(p):
    return {k: p[k] for k in ("reference", "manifest", "questions", "output")}


def reseal(p):
    p["reference"].write_text(json.dumps(p["doc"], ensure_ascii=False))
    meta = json.loads(p["manifest"].read_text())
    meta["reference_sha256"] = E.sha(p["reference"])
    p["manifest"].write_text(json.dumps(meta))


class Client:
    mock = False  # synthetic fixture client; never used with real data

    def __init__(self, p, *, omit=None, fail=None, ambiguous=None):
        self.bank = p["bank"]
        self.by_text = {c["state"]["评论原文"]: c for c in p["doc"]["cases"]}
        self.calls = []
        self.omit, self.fail, self.ambiguous = omit, fail, ambiguous

    def call(self, body):
        c = self.by_text[body["state"]["评论原文"]]
        self.calls.append(body)
        if c["id"] == self.fail:
            raise E.JevError("private prompt and secret must never appear in artifacts")
        answers = {}
        for q in self.bank.questions:
            if c["id"] == self.omit and q.id == "names_brand":
                continue
            label = c["expect"][q.id]
            if q.jtype == "noul":
                p_yes = 0.9 if label == "是" else 0.1
                if c["id"] == self.ambiguous:
                    p_yes = 0.55 if label == "是" else 0.45
                answers[q.id] = {"type": "noul", "noul": p_yes}
            else:
                answers[q.id] = {"type": "choice", "choice": label,
                    "probabilities": {v: 0.98 if v == label else 0.001 for v in q.criteria}}
        return {"answers": answers}


def test_all_700_labels_use_own_wording_and_ai_reference_is_not_human_accuracy(packet):
    client = Client(packet)
    assert E.evaluate(**arguments(packet), client=client) == 0
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["answer_agreement_cells"] == summary["unambiguous_agreement_cells"] == 700
    assert summary["metrics_kind"].endswith("not_independent_human_accuracy")
    assert not summary["accepted"] and not summary["release_accepted"]
    by_text = {c["state"]["评论原文"]: c for c in packet["doc"]["cases"]}
    for body in client.calls:
        case = by_text[body["state"]["评论原文"]]
        assert body["questions"] == E.build_questions(packet["bank"], fill=case["fill"])
    assert len({body["questions"]["echoes_post"]["instructions"] for body in client.calls}) == 100
    assert not any("合成" in path.read_text() for path in packet["output"].glob("*.json"))


def test_missing_false_label_cannot_hit_or_drop_from_denominator(packet):
    assert E.evaluate(**arguments(packet), client=Client(packet, omit="c3", fail="c4")) == 1
    s = json.loads((packet["output"] / "summary.json").read_text())
    assert s["cells"] == 700 and s["answer_agreement_cells"] == 692
    assert s["invalid_or_missing_cells"] == 8 and s["called_cases"] == 100
    assert s["per_question"]["names_brand"]["total"] == 100
    assert "private prompt" not in (packet["output"] / "checkpoint.json").read_text()


def test_ambiguous_hits_are_separate_from_usable_hits(packet):
    assert E.evaluate(**arguments(packet), client=Client(packet, ambiguous="c2")) == 0
    s = json.loads((packet["output"] / "summary.json").read_text())
    assert s["answer_agreement_cells"] == 700 and s["unambiguous_agreement_cells"] == 697
    assert s["ambiguous_cells"] == 3


def test_resume_retries_invalid_and_failed_cases_without_rerunning_success(packet):
    assert E.evaluate(**arguments(packet), client=Client(packet, omit="c3", fail="c4")) == 1
    checkpoint = packet["output"] / "checkpoint.json"
    packet["output"] = packet["output"].parent / "resume-output"
    client = Client(packet)
    assert E.evaluate(**arguments(packet), resume=checkpoint, client=client) == 0
    assert len(client.calls) == 2
    data = json.loads(checkpoint.read_text())
    data["live"] = False
    checkpoint.write_text(json.dumps(data))
    packet["output"] = packet["output"].parent / "bad-resume"
    with pytest.raises(ValueError, match="non-live"):
        E.evaluate(**arguments(packet), resume=checkpoint, client=Client(packet))


def test_complete_live_resume_recovers_report_without_credentials_or_model_calls(packet, monkeypatch):
    assert E.evaluate(**arguments(packet), client=Client(packet)) == 0
    checkpoint = packet["output"] / "checkpoint.json"
    packet["output"] = packet["output"].parent / "recovered"
    def must_not_construct_client():
        raise AssertionError("complete resume needs no client")
    monkeypatch.setattr(E, "JevClient", must_not_construct_client)
    assert E.evaluate(**arguments(packet), resume=checkpoint) == 0
    assert json.loads((packet["output"] / "summary.json").read_text())["answer_agreement_cells"] == 700


@pytest.mark.parametrize("metadata", [None, {}, {"human_arbitration_claimed": False}])
def test_provenance_metadata_required_before_any_calls(packet, metadata):
    packet["doc"]["reference_provenance"] = metadata
    reseal(packet)
    client = Client(packet)
    with pytest.raises(ValueError, match="provenance"):
        E.evaluate(**arguments(packet), client=client)
    assert not client.calls


@pytest.mark.parametrize("bad_probability", [float("nan"), float("inf"), -0.1, 1.1])
def test_nonselected_probability_is_validated_before_interpret(packet, bad_probability):
    original = Client(packet)
    class BadClient:
        mock = False
        def call(self, body):
            response = original.call(body)
            if body["state"]["评论原文"] == packet["doc"]["cases"][0]["state"]["评论原文"]:
                answer = response["answers"]["speech_act"]
                other = next(label for label in answer["probabilities"] if label != answer["choice"])
                answer["probabilities"][other] = bad_probability
            return response
    assert E.evaluate(**arguments(packet), client=BadClient()) == 1
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["invalid_or_missing_cells"] == 1 and summary["answer_agreement_cells"] == 699


@pytest.mark.parametrize("mutation", [
    lambda d: d["cases"][1]["state"].update({"评论原文": "changed"}),
    lambda d: d["cases"][1]["fill"].update({"post_points": "another post"}),
    lambda d: d["cases"][1]["expect"].pop("arranged"),
    lambda d: d["cases"][1]["expect"].update({"arranged": "maybe"}),
    lambda d: d["cases"].pop(),
    lambda d: d.update({"label_decisions_final": False}),
])
def test_bad_binding_or_labels_rejected_before_client_calls(packet, mutation):
    mutation(packet["doc"])
    reseal(packet)
    client = Client(packet)
    with pytest.raises(ValueError):
        E.evaluate(**arguments(packet), client=client)
    assert not client.calls and not packet["output"].exists()


def test_missing_credential_is_blocked_and_does_not_emit_fake_metrics(packet, monkeypatch):
    def absent():
        raise E.JevError("missing key")
    monkeypatch.setattr(E, "JevClient", absent)
    assert E.evaluate(**arguments(packet)) == 4
    status = json.loads((packet["output"] / "status.json").read_text())
    assert status["status"] == "blocked_missing_jev_credentials" and status["model_calls"] == 0
    assert not (packet["output"] / "summary.json").exists()


def test_nonfinite_probabilities_and_explicit_invalid_are_not_matches(packet):
    q = packet["bank"].by_id()["names_brand"]
    for prob in (float("nan"), float("inf"), -0.1, 1.1, True):
        assert E.safe_item({"answer": "否", "p": prob, "p_yes": 0.1, "ambiguous": False}, q)["invalid"]
    assert E.safe_item({"answer": "否", "p": 0.9, "p_yes": 0.1, "ambiguous": False, "invalid": True}, q)["invalid"]


def test_nonempty_outputs_and_git_paths_are_protected(packet):
    packet["output"].mkdir()
    (packet["output"] / "human-work.txt").write_text("keep")
    with pytest.raises(ValueError, match="not empty"):
        E.evaluate(**arguments(packet), client=Client(packet))
    packet["output"] = E.ROOT / "evaluation-artifacts"
    with pytest.raises(ValueError, match="outside"):
        E.evaluate(**arguments(packet), client=Client(packet))


def test_risk_signals_include_unambiguous_misses_but_do_not_claim_api_pass(packet):
    results = {}
    for case in packet["doc"]["cases"]:
        items = {}
        for q in packet["bank"].questions:
            it = {"answer": case["expect"][q.id], "p": 0.9, "ambiguous": False}
            if q.jtype == "noul":
                it["p_yes"] = 0.9 if it["answer"] == "是" else 0.1
            items[q.id] = it
        results[case["id"]] = {"status": "ok", "items": items}
    results["c0"]["items"]["speech_act"]["answer"] = "补充经验"
    results["c1"]["items"]["arranged"].update({"answer": "否", "p_yes": 0.1})
    s = E.summarize(packet["bank"], packet["doc"], results)
    assert s["reader_bank_risk_signals"]["endorsement_unambiguous_nonendorsement"] == ["c0"]
    assert s["reader_bank_risk_signals"]["arranged_unambiguous_false_negative"] == ["c1"]
    assert not s["release_accepted"]


@pytest.mark.parametrize("path", [CANDIDATE, HISTORICAL_CANDIDATE])
def test_candidate_is_offline_and_only_two_wordings_change(path):
    baseline = load_bank(E.BANK)
    candidate, audit = E.candidate_for(path, baseline)
    assert audit["changed_questions"] == ["speech_act", "detail_level"]
    assert candidate.model == baseline.model and candidate.ambiguity == baseline.ambiguity
    assert candidate.by_id()["arranged"] == baseline.by_id()["arranged"]
    assert path.stem not in discover(E.ROOT / "banks")
    if path == HISTORICAL_CANDIDATE:
        assert candidate.sha256 == "82d159dc2481afecf813d0e61c280aece2623e5dcff9e8444656ffc49b488f64"


def test_candidate_development_run_keeps_frozen_labels_and_binds_both_banks(packet):
    before = E.sha(packet["reference"])
    client = Client(packet)
    assert E.evaluate(**arguments(packet), client=client, candidate=CANDIDATE) == 0
    summary = json.loads((packet["output"] / "summary.json").read_text())
    assert summary["metrics_kind"].endswith("not_candidate_acceptance")
    assert summary["candidate_experiment"]["owner_definition_review_pending"] is True
    assert not summary["release_accepted"]
    assert E.sha(packet["reference"]) == before
    candidate = load_bank(CANDIDATE)
    assert summary["binding"]["bank_sha256"] == candidate.sha256
    assert summary["binding"]["reference_bank_sha256"] == packet["bank"].sha256
    for body in client.calls:
        case = client.by_text[body["state"]["评论原文"]]
        assert body["questions"] == E.build_questions(candidate, fill=case["fill"])


def test_baseline_checkpoint_cannot_be_reused_by_candidate(packet):
    assert E.evaluate(**arguments(packet), client=Client(packet)) == 0
    resume = packet["output"] / "checkpoint.json"
    packet["output"] = packet["output"].parent / "candidate"
    client = Client(packet)
    with pytest.raises(ValueError, match="mismatch"):
        E.evaluate(**arguments(packet), client=client, candidate=CANDIDATE, resume=resume)
    assert not client.calls


@pytest.mark.parametrize("mutation", [
    lambda d: d["ambiguity"].update({"choice_top_min": 0.1}),
    lambda d: d.update({"model": "jev-latest"}),
    lambda d: d["questions"][-1]["instructions"].update({"zh": "changed arranged"}),
    lambda d: d["questions"][0]["options"].pop(),
    lambda d: d["questions"][0].update({"on_ambiguous": "pass"}),
    lambda d: d["questions"][0]["instructions"].update({"zh": "new missing {extra_context}"}),
    lambda d: d["questions"][4]["options"].reverse(),
])
def test_candidate_scope_and_missing_fill_rejected_before_calls(packet, tmp_path, monkeypatch, mutation):
    root = tmp_path / "offline"
    root.mkdir()
    path = root / "candidate.yaml"
    data = yaml.safe_load(CANDIDATE.read_text())
    mutation(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    monkeypatch.setattr(E, "CANDIDATE_ROOT", root)
    client = Client(packet)
    with pytest.raises(ValueError):
        E.evaluate(**arguments(packet), client=client, candidate=path)
    assert not client.calls
