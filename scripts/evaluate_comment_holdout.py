#!/usr/bin/env python3
"""Local-only evaluation of the frozen 40-case single-reviewer C3 reference.

The original 100-case validator remains unchanged. No database or release writes.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import build_questions, load_bank
from judge.comments import fill_for
from judge.core import judge_state
from judge.jev_client import JevClient, JevError
from scripts.backfill_comments import post_for, state_reader
from scripts import evaluate_comment_reference as E

VERSION = "comment-independent-holdout-eval-v1"
BANK = E.ROOT / "banks/candidates/comment_reader_v0.5_candidate.yaml"
SCOPE = "single_reviewer_returned_judgments"
BOUND = ("reference_version", "sample_version", "sample_sha256", "development_sample_sha256",
         "bank_version", "bank_sha256", "filled_questions_sha256", "review_workbook_sha256", "model", "reference_scope")


def load_reference(reference: Path, manifest: Path, questions: Path, sample: Path,
                   development_sample: Path, review_workbook: Path):
    doc = json.loads(reference.read_text(encoding="utf-8"))
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    packet = json.loads(questions.read_text(encoding="utf-8"))
    samples = json.loads(sample.read_text(encoding="utf-8"))
    dev = json.loads(development_sample.read_text(encoding="utf-8"))
    bank, _ = E.candidate_for(BANK, load_bank(E.BANK))
    if bank.version != "cb-v0.5-candidate3":
        raise ValueError("holdout evaluator only supports candidate3")
    if any(not doc.get(k) or doc[k] != meta.get(k) for k in BOUND):
        raise ValueError("holdout manifest binding mismatch")
    checks = {"reference_sha256": reference, "sample_sha256": sample,
              "development_sample_sha256": development_sample,
              "filled_questions_sha256": questions, "review_workbook_sha256": review_workbook}
    if any(meta.get(key) != E.sha(path) for key, path in checks.items()):
        raise ValueError("holdout source digest mismatch")
    if (doc["bank_sha256"] != bank.sha256 or doc["bank_version"] != bank.version
            or doc["model"] != bank.model or samples.get("sample_version") != doc["sample_version"]):
        raise ValueError("holdout bank/model/sample mismatch")
    if (doc.get("reference_ready") is not True or doc.get("label_decisions_final") is not True
            or meta.get("labels_frozen") is not True or doc["reference_scope"] != SCOPE
            or any(doc.get(k) is not False for k in ("accepted", "release_accepted", "human_gold_ready"))):
        raise ValueError("holdout reference state or scope invalid")
    if (type(meta.get("count")) is not int or meta["count"] != 40
            or type(meta.get("cells")) is not int or meta["cells"] != 280
            or samples.get("used_for_prompt_design") is not False
            or samples.get("reference_labels") is not None
            or type(samples.get("model_calls")) is not int or samples["model_calls"] != 0
            or type(doc.get("model_calls_before_reference_freeze")) is not int
            or doc["model_calls_before_reference_freeze"] != 0):
        raise ValueError("holdout counts or pre-evaluation independence state invalid")
    origin = doc.get("reference_provenance", {})
    if (origin.get("kind") != "single_reviewer_returned_labels" or not isinstance(origin.get("reviewer"), str)
            or not origin["reviewer"].strip() or type(origin.get("reviewer_count")) is not int
            or origin["reviewer_count"] != 1 or type(origin.get("reviewer_label_cells")) is not int
            or origin["reviewer_label_cells"] != 280 or type(origin.get("AI_label_overrides")) is not int
            or origin["AI_label_overrides"] != 0 or origin.get("definition_conformance_accepted") is not False):
        raise ValueError("single-reviewer source metadata invalid")
    cases, rows, devrows = doc.get("cases"), samples.get("notes"), dev.get("notes")
    for group, key in ((cases, "id"), (packet, "comment_id"), (rows, "comment_id")):
        if (not isinstance(group, list) or len(group) != 40
                or any(not isinstance(c, dict) or not isinstance(c.get(key), str) or not c[key] for c in group)
                or len({c[key] for c in group}) != 40):
            raise ValueError("holdout needs exactly 40 unique cases")
    if (not isinstance(devrows, list) or len(devrows) != 100
            or any(not isinstance(r, dict) or not isinstance(r.get("comment_id"), str) for r in devrows)
            or len({r["comment_id"] for r in devrows}) != 100):
        raise ValueError("development sample must retain its 100 cases")
    ids = {c["id"] for c in cases}
    if ids != {c["comment_id"] for c in packet} or ids != {c["comment_id"] for c in rows}:
        raise ValueError("holdout ID sets differ")
    if any(not isinstance(r.get("note_id"), str) or not isinstance(r.get("comment_text"), str) for r in rows + devrows):
        raise ValueError("source rows malformed")
    posts = {r["note_id"] for r in rows}
    def fingerprint(row):
        return (row.get("project_id") or row["note_id"].split("_rec", 1)[0], row["comment_text"].strip())
    if (len(posts) != 40 or len({fingerprint(r) for r in rows}) != 40
            or posts & {r["note_id"] for r in devrows}
            or {fingerprint(r) for r in rows} & {fingerprint(r) for r in devrows}):
        raise ValueError("holdout overlaps development sample or repeats posts")
    source = {r["comment_id"]: r for r in rows}
    rendered = {r["comment_id"]: r for r in packet}
    for case in cases:
        row = source[case["id"]]
        if (case["state"] != state_reader(row) or case["fill"] != fill_for(post_for(row))
                or case["state"] != rendered[case["id"]]["state"]
                or case["fill"] != rendered[case["id"]]["fill"]
                or build_questions(bank, fill=case["fill"]) != rendered[case["id"]]["questions"]):
            raise ValueError("holdout source context or filled wording differs")
        if set(case.get("expect", {})) != set(bank.ids()) or set(case.get("provenance", {})) != set(bank.ids()):
            raise ValueError("holdout needs seven labels and source records per case")
        for q in bank.questions:
            label = case["expect"][q.id]
            allowed = ["是", "否"] if q.jtype == "noul" else list(q.criteria)
            provenance = case["provenance"][q.id]
            if (not isinstance(label, str) or label not in allowed or not isinstance(provenance, dict)
                    or provenance.get("kind") != "returned_reviewer_label"
                    or provenance.get("reviewer") != origin["reviewer"]):
                raise ValueError("holdout label/source invalid")
    binding = {k: doc[k] for k in BOUND}
    binding.update(reference_sha256=E.sha(reference), evaluator_version=VERSION)
    return bank, doc, binding


def output_target(output: Path):
    target = output.resolve()
    if target.is_relative_to(E.ROOT.resolve()) or any(
            (p / ".git").is_file() or (p / ".git/HEAD").is_file() for p in (target, *target.parents)):
        raise ValueError("holdout artifacts must remain outside Git repositories")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("use an empty output directory")


def evaluate(reference, manifest, questions, sample, development_sample, review_workbook,
             output, *, workers=4, resume=None, client=None, validate_only=False, authorization=None):
    output_target(output)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    bank, doc, binding = load_reference(reference, manifest, questions, sample, development_sample, review_workbook)
    results = E.validate_resume(resume, binding, bank, {c["id"] for c in doc["cases"]})
    pending = [c for c in doc["cases"] if c["id"] not in results]
    output.mkdir(parents=True, exist_ok=True)
    if validate_only:
        E.save_json(output / "status.json", {"status": "holdout_validated_no_calls", "binding": binding,
                    "count": 40, "cells": 280, "model_calls": 0, "live": False, "release_accepted": False})
        return 0
    if pending:
        if authorization is None:
            E.save_json(output / "status.json", {"status": "blocked_missing_batch_authorization", "binding": binding,
                        "model_calls": 0, "live": False, "release_accepted": False})
            return 5
        receipt = json.loads(authorization.read_text(encoding="utf-8"))
        if (receipt.get("approved") is not True or receipt.get("scope") != "one_independent_holdout_candidate3_batch"
                or receipt.get("binding") != binding or type(receipt.get("max_cases")) is not int
                or receipt["max_cases"] != 40 or not receipt.get("owner_instruction")
                or receipt.get("output_directory") != str(output.resolve())):
            raise ValueError("batch authorization scope or binding mismatch")
        try:
            client = E.ProbabilityCheckedClient(client if client is not None else JevClient())
        except JevError:
            E.save_json(output / "status.json", {"status": "blocked_missing_jev_credentials", "binding": binding,
                        "model_calls": 0, "live": False, "release_accepted": False})
            return 4
    live = not bool(getattr(client, "mock", False))
    if results and not live:
        raise ValueError("cannot mix live checkpoint and mock client")

    def run(case):
        try:
            result = judge_state(client, bank, case["id"], case["state"], fill=case["fill"])
            items = {q.id: E.safe_item(result.items.get(q.id), q) for q in bank.questions}
            return {"status": "invalid" if any(x["invalid"] for x in items.values()) else "ok", "items": items}
        except Exception as exc:
            return {"status": "error", "error_kind": "jev_call_failed" if isinstance(exc, JevError) else "invalid_response", "items": {}}

    def checkpoint():
        E.save_json(output / "checkpoint.json", {"binding": binding, "live": live, "results": results})
    checkpoint()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(run, c): c["id"] for c in pending}
        for future in cf.as_completed(tasks):
            results[tasks[future]] = future.result()
            checkpoint()
    summary = E.summarize(bank, doc, results)
    # Include both sides of the endorsement boundary; reducing false negatives
    # must not hide additional false positives as happened in the C1 dev run.
    extras = {"nonendorsement_reference_cases": 0, "endorsement_false_positive_all_valid": [],
              "endorsement_unambiguous_false_positive": [], "endorsement_false_negative_all_valid": []}
    for case in doc["cases"]:
        expected = case["expect"]["speech_act"] in ("亲历背书", "旁观推荐")
        extras["nonendorsement_reference_cases"] += not expected
        item = E.safe_item(results.get(case["id"], {}).get("items", {}).get("speech_act"), bank.by_id()["speech_act"])
        if item["invalid"]:
            continue
        predicted = item["answer"] in ("亲历背书", "旁观推荐")
        if not expected and predicted:
            extras["endorsement_false_positive_all_valid"].append(case["id"])
            if not item["ambiguous"]:
                extras["endorsement_unambiguous_false_positive"].append(case["id"])
        if expected and not predicted:
            extras["endorsement_false_negative_all_valid"].append(case["id"])
    summary["reader_bank_risk_signals"].update(extras)
    summary.update(binding=binding, live=live, model_calls_this_run=len(pending),
                   status="evaluation_complete" if all(r["status"] == "ok" for r in results.values()) else "evaluation_incomplete",
                   metrics_kind="model_agreement_with_single_reviewer_returned_holdout_not_release_acceptance",
                   reference_definition_caveats=doc.get("definition_caveats", []), human_gold_ready=False,
                   statistical_comparison_with_old100_valid=False)
    E.save_json(output / "summary.json", summary)
    lines = [f"# 独立40条对照 · {bank.version} · {bank.model}", "",
             "参考是一位复核人员的返回原判，保留题意解释差异；不称无争议金标准或上线验收。",
             "与旧100条样本/参考定义不同，不能把两次总分差直接当作候选提升。非live结果不用于模型验收。",
             "固定40条/280格，缺答/无效仍计入分母。", "",
             "| 题 | 一致/40 | 无歧义一致 | 缺答/无效 | 歧义 |", "|---|---|---|---|---|"]
    for qid, m in summary["per_question"].items():
        lines.append(f"| {qid} | {m['answer_agreement']} | {m['unambiguous_agreement']} | {m['invalid_or_missing']} | {m['ambiguous']} |")
    lines += ["", f"答案一致 {summary['answer_agreement_cells']}/280；无歧义一致 {summary['unambiguous_agreement_cells']}/280。",
              "读者题库风险信号见summary.json，不等于完整HTTP放行结果。不会改参考、题库、账本或开关。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if summary["status"] != "evaluation_complete" else (0 if summary["answer_agreement_cells"] == 280 else 3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "manifest", "questions", "sample", "development-sample", "review-workbook", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--authorization", type=Path, help="local receipt of the owner's permission for this bound batch")
    try:
        return evaluate(**vars(parser.parse_args(argv)))
    except (ValueError, KeyError, TypeError, OSError):
        print("invalid holdout evaluation input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
