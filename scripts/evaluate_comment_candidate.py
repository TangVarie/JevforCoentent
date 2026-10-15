#!/usr/bin/env python3
"""Bounded C4 diagnosis; legacy C1/C3 validators and production gates stay intact."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import load_bank
from judge.comments import fill_for
from judge.core import judge_state
from judge.jev_client import JevClient, JevError
from scripts.backfill_comments import post_for, state_reader
from scripts import evaluate_comment_reference as E
from scripts.evaluate_comment_holdout import output_target

BANK = E.CANDIDATE_ROOT / "comment_reader_v0.6_candidate4.yaml"
BASE = E.CANDIDATE_ROOT / "comment_reader_v0.5_candidate.yaml"
VERSION = "comment-candidate4-diagnostic-v2"
CONTRACT = E.ROOT / "config/comment_candidate4_diagnostic_contract.json"
SCOPES = {"development_single_reviewer", "unseen_AI_definition_reference", "synthetic_specification"}


def candidate():
    bank, old = load_bank(BANK), load_bank(BASE)
    if (bank.version != "cb-v0.6-candidate4" or bank.model != old.model
            or bank.ids() != old.ids() or bank.ambiguity != old.ambiguity
            or bank.fill_defaults != old.fill_defaults):
        raise ValueError("C4 may only clarify wording")
    for q, previous in zip(bank.questions, old.questions):
        if q.jtype != previous.jtype or list(q.criteria) != list(previous.criteria):
            raise ValueError("C4 question types and ordered labels must be preserved")
        for field in ("scope", "evidence", "unclear_labels", "feeds", "on_ambiguous", "version", "aw_instruction"):
            if getattr(q, field) != getattr(previous, field):
                raise ValueError("C4 operational contract changed")
    return bank


def read_packet(sample, reference, protocol, source_reference=None):
    bank = candidate()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if contract["candidate_version"] != bank.version or contract["bank_sha256"] != bank.sha256:
        raise ValueError("trusted C4 contract differs from candidate")
    sample_doc = json.loads(sample.read_text(encoding="utf-8"))
    doc = json.loads(reference.read_text(encoding="utf-8"))
    plan = json.loads(protocol.read_text(encoding="utf-8"))
    thresholds = plan.get("diagnostic_thresholds")
    required = {"overall_agreement_min", "per_question_agreement_min", "ambiguous_fraction_max",
                "unambiguous_endorsement_false_negatives_max"}
    if not isinstance(thresholds, dict) or set(thresholds) != required:
        raise ValueError("diagnostic threshold schema incomplete")
    for key in required - {"unambiguous_endorsement_false_negatives_max"}:
        value = thresholds[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("diagnostic threshold must be a finite numeric fraction")
    misses = thresholds["unambiguous_endorsement_false_negatives_max"]
    if type(misses) is not int or misses < 0:
        raise ValueError("diagnostic false-negative limit must be a nonnegative integer")
    if thresholds != contract["diagnostic_thresholds"]:
        raise ValueError("thresholds differ from trusted pre-registered values")
    freeze_path = Path(plan["design_freeze"]["path"])
    if E.sha(freeze_path) != plan["design_freeze"]["sha256"]:
        raise ValueError("pre-registered design digest mismatch")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if (freeze.get("bank_sha256") != bank.sha256 or freeze.get("candidate") != bank.version
            or freeze.get("model_diagnostic_gate") != plan.get("diagnostic_thresholds")):
        raise ValueError("diagnostic protocol differs from pre-registered design")
    scope, count = plan.get("scope"), plan.get("count")
    if scope not in SCOPES or type(count) is not int or count != contract["scope_counts"][scope]:
        raise ValueError("invalid evaluation scope or count")
    if (plan.get("sample_sha256") != E.sha(sample) or plan.get("reference_sha256") != E.sha(reference)
            or plan.get("bank_sha256") != bank.sha256 or plan.get("bank_version") != bank.version
            or plan.get("model") != bank.model or plan.get("cells") != count * len(bank.questions)):
        raise ValueError("packet digest/model mismatch")
    if (doc.get("reference_ready") is not True or doc.get("model_calls_before_freeze") != 0
            or type(doc.get("model_calls_before_freeze")) is not int
            or doc.get("scope") != scope or doc.get("bank_sha256") != bank.sha256
            or any(doc.get(k) is not False for k in ("human_gold_ready", "release_accepted", "accepted"))):
        raise ValueError("reference readiness/provenance state invalid")
    expected_kind = "returned_reviewer_label" if scope == "development_single_reviewer" else "AI_definition_reference"
    if doc.get("source_kind") != expected_kind or not doc.get("reference_author"):
        raise ValueError("reference source missing or relabelled")
    origin = doc.get("reference_provenance", {})
    if (origin.get("kind") != expected_kind or origin.get("cells") != count * len(bank.questions)
            or type(origin.get("cells")) is not int or origin.get("independent_human_gold") is not False):
        raise ValueError("reference provenance malformed")
    rows, cases = sample_doc.get("notes"), doc.get("cases")
    for group, key in ((rows, "comment_id"), (cases, "id")):
        if (not isinstance(group, list) or len(group) != count
                or any(not isinstance(c, dict) or not isinstance(c.get(key), str) or not c[key] for c in group)
                or len({c[key] for c in group}) != count):
            raise ValueError("need unique complete sample/reference IDs")
    if {r["comment_id"] for r in rows} != {c["id"] for c in cases}:
        raise ValueError("sample/reference ID sets differ")
    if scope == "development_single_reviewer":
        if (source_reference is None or E.sha(source_reference) != contract["returned_reference_sha256"]
                or plan.get("source_reference_sha256") != contract["returned_reference_sha256"]):
            raise ValueError("development reference needs unchanged source")
        original = json.loads(source_reference.read_text(encoding="utf-8"))
        old = {c["id"]: c["expect"] for c in original["cases"]}
        if any(old.get(c["id"]) != c["expect"] for c in cases):
            raise ValueError("returned reviewer labels must not be overwritten")
    if scope == "unseen_AI_definition_reference":
        if (sample_doc.get("used_for_prompt_design") is not False or sample_doc.get("model_calls") != 0
                or type(sample_doc.get("model_calls")) is not int
                or sample_doc.get("reference_labels") is not None or not plan.get("excluded_samples")):
            raise ValueError("unseen scope needs a frozen pre-model sample and exclusions")
        def fingerprint(row):
            return (row["project_id"], row["comment_text"].strip())
        posts = {r["note_id"] for r in rows}
        texts = {fingerprint(r) for r in rows}
        if len(posts) != count or len(texts) != count:
            raise ValueError("unseen sample repeats posts/text")
        for entry in plan["excluded_samples"]:
            path = Path(entry["path"])
            if E.sha(path) != entry["sha256"]:
                raise ValueError("excluded sample digest mismatch")
            excluded = json.loads(path.read_text(encoding="utf-8"))["notes"]
            if posts & {r["note_id"] for r in excluded} or texts & {fingerprint(r) for r in excluded}:
                raise ValueError("unseen sample overlaps development data")
    source = {r["comment_id"]: r for r in rows}
    for c in cases:
        row = source[c["id"]]
        if c.get("state") != state_reader(row) or c.get("fill") != fill_for(post_for(row)):
            raise ValueError("state/fill differs from fixed source")
        if set(c.get("expect", {})) != set(bank.ids()):
            raise ValueError("each case needs all seven closed labels")
        for q in bank.questions:
            allowed = ["是", "否"] if q.jtype == "noul" else list(q.criteria)
            if c["expect"][q.id] not in allowed:
                raise ValueError("reference label outside closed vocabulary")
    binding = {"sample_sha256": E.sha(sample), "reference_sha256": E.sha(reference),
               "protocol_sha256": E.sha(protocol), "bank_sha256": bank.sha256,
               "trusted_contract_sha256": E.sha(CONTRACT),
               "bank_version": bank.version, "model": bank.model, "scope": scope,
               "count": count, "evaluator_version": VERSION}
    return bank, doc, plan, binding


def evaluate(sample, reference, protocol, output, *, source_reference=None, authorization=None,
             resume=None, validate_only=False, client=None, workers=4):
    output_target(output)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    bank, doc, plan, binding = read_packet(sample, reference, protocol, source_reference)
    results = E.validate_resume(resume, binding, bank, {c["id"] for c in doc["cases"]})
    pending = [c for c in doc["cases"] if c["id"] not in results]
    output.mkdir(parents=True, exist_ok=True)
    if validate_only:
        E.save_json(output / "status.json", {"status": "validated_no_calls", "binding": binding, "model_calls": 0})
        return 0
    if pending:
        if authorization is None:
            E.save_json(output / "status.json", {"status": "blocked_no_authorization", "model_calls": 0})
            return 5
        receipt = json.loads(authorization.read_text(encoding="utf-8"))
        if (receipt.get("approved") is not True or receipt.get("binding") != binding
                or receipt.get("output_directory") != str(output.resolve())
                or receipt.get("max_cases") != binding["count"] or type(receipt.get("max_cases")) is not int
                or not receipt.get("owner_instruction")):
            raise ValueError("authorization not bound to this one batch")
        try:
            client = E.ProbabilityCheckedClient(client if client is not None else JevClient(),
                                                require_all_choice_labels=True)
        except JevError:
            E.save_json(output / "status.json", {"status": "blocked_missing_credentials", "model_calls": 0})
            return 4
    live = not bool(getattr(client, "mock", False))
    if results and not live:
        raise ValueError("cannot mix live checkpoint and mock client")

    def run(c):
        try:
            judged = judge_state(client, bank, c["id"], c["state"], fill=c["fill"])
            items = {q.id: E.safe_item(judged.items.get(q.id), q) for q in bank.questions}
            return {"status": "invalid" if any(i["invalid"] for i in items.values()) else "ok", "items": items}
        except Exception as exc:
            return {"status": "error", "error_kind": "jev_call_failed" if isinstance(exc, JevError) else "invalid_response", "items": {}}

    def checkpoint():
        E.save_json(output / "checkpoint.json", {"binding": binding, "live": live, "results": results})
    checkpoint()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, c): c["id"] for c in pending}
        for future in cf.as_completed(futures):
            results[futures[future]] = future.result()
            checkpoint()
    summary = E.summarize(bank, doc, results)
    risks = {"reference_positive": 0, "reference_negative": 0, "false_negative": [],
             "false_positive": [], "unambiguous_false_negative": [], "unambiguous_false_positive": []}
    for c in doc["cases"]:
        expected = c["expect"]["speech_act"] in ("亲历背书", "旁观推荐")
        risks["reference_positive" if expected else "reference_negative"] += 1
        item = E.safe_item(results.get(c["id"], {}).get("items", {}).get("speech_act"), bank.by_id()["speech_act"])
        if item["invalid"]:
            continue
        predicted = item["answer"] in ("亲历背书", "旁观推荐")
        if expected != predicted:
            key = "false_negative" if expected else "false_positive"
            risks[key].append(c["id"])
            if not item["ambiguous"]:
                risks["unambiguous_" + key].append(c["id"])
    thresholds = plan["diagnostic_thresholds"]
    reasons = []
    if not live:
        reasons.append("non_live")
    if summary["invalid_or_missing_cells"]:
        reasons.append("invalid_or_missing")
    if summary["answer_agreement_cells"] / summary["cells"] < thresholds["overall_agreement_min"]:
        reasons.append("overall_agreement")
    if any(m["answer_agreement"] / m["total"] < thresholds["per_question_agreement_min"] for m in summary["per_question"].values()):
        reasons.append("per_question_agreement")
    if summary["ambiguous_cells"] / summary["cells"] > thresholds["ambiguous_fraction_max"]:
        reasons.append("ambiguity")
    if len(risks["unambiguous_false_negative"]) > thresholds["unambiguous_endorsement_false_negatives_max"]:
        reasons.append("endorsement_false_negative")
    complete = len(results) == binding["count"] and all(r["status"] == "ok" for r in results.values())
    summary.update(binding=binding, live=live, model_calls_this_run=len(pending),
                   status="evaluation_complete" if complete else "evaluation_incomplete",
                   metrics_kind=binding["scope"] + "_agreement_not_release_acceptance",
                   reference_source=doc["source_kind"], human_gold_ready=False,
                   endorsement_boundary=risks,
                   diagnostic_gate={"passed": not reasons, "reasons": reasons, "thresholds": thresholds},
                   release_decision="hold_pending_actual_acceptance", release_accepted=False)
    E.save_json(output / "summary.json", summary)
    return (1 if not complete else 0 if not reasons else 3)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("sample", "reference", "protocol", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    for name in ("source-reference", "authorization", "resume"):
        p.add_argument("--" + name, type=Path)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    try:
        return evaluate(**vars(p.parse_args(argv)))
    except (ValueError, KeyError, TypeError, OSError):
        print("invalid C4 diagnostic input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
