#!/usr/bin/env python3
"""Evaluate a frozen 100-comment reference, with per-case wording and no database writes.

References may include owner-adopted AI arbitration: report that provenance honestly.
No CLI mock mode, no acceptance record, no feature-flag change. Source text stays local.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import build_questions, load_bank, unfilled
from judge.core import judge_state
from judge.jev_client import JevClient, JevError
from scripts.ingest_comment_annotations import normalize_answer

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "banks/comment_reader_v0.4.yaml"
INVALID = "<invalid>"
EVALUATOR_VERSION = "comment-reference-eval-v2"
CANDIDATE_ROOT = ROOT / "banks/candidates"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reference(reference: Path, manifest: Path, questions: Path):
    doc = json.loads(reference.read_text(encoding="utf-8"))
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    bank = load_bank(BANK)
    if meta.get("reference_sha256") != sha(reference):
        raise ValueError("reference digest mismatch")
    if doc.get("reference_ready") is not True or doc.get("label_decisions_final") is not True:
        raise ValueError("reference labels have not been finalized")
    provenance = doc.get("reference_provenance")
    if not isinstance(provenance, dict) or type(provenance.get("human_arbitration_claimed")) is not bool:
        raise ValueError("reference provenance missing or malformed")
    for key in ("AB_agreed_cells", "AI_arbitration_cells_adopted_by_user"):
        if type(provenance.get(key)) is not int or not 0 <= provenance[key] <= 700:
            raise ValueError("reference provenance counts missing or malformed")
    for key in ("reference_version", "sample_version", "sample_sha256", "bank_sha256", "filled_questions_sha256", "model"):
        if not doc.get(key) or doc[key] != meta.get(key):
            raise ValueError(f"manifest binding mismatch: {key}")
    if doc["bank_sha256"] != bank.sha256 or doc["model"] != bank.model or sha(questions) != doc["filled_questions_sha256"]:
        raise ValueError("current bank/model/filled questions do not match reference")
    cases = doc.get("cases")
    if not isinstance(cases, list) or len(cases) != 100 or len({c["id"] for c in cases}) != 100:
        raise ValueError("reference must contain exactly 100 unique cases")
    original = json.loads(questions.read_text(encoding="utf-8"))
    if not isinstance(original, list) or len(original) != 100:
        raise ValueError("filled question artifact must contain 100 cases")
    by_id = {c["comment_id"]: c for c in original}
    if set(by_id) != {c["id"] for c in cases}:
        raise ValueError("filled question IDs differ")
    for c in cases:
        if set(c.get("expect", {})) != set(bank.ids()) or set(c.get("provenance", {})) != set(bank.ids()):
            raise ValueError("each case needs seven final labels and provenance records")
        for q in bank.questions:
            normalize_answer(c["expect"][q.id], q)
            origin = c["provenance"][q.id]
            if not isinstance(origin, dict) or origin.get("kind") not in ("AB_agreement", "user_adopted_final_arbitration"):
                raise ValueError("case provenance missing or malformed")
        fill = c.get("fill")
        if not isinstance(fill, dict) or unfilled(bank, fill):
            raise ValueError("per-case question fill incomplete")
        if c["state"] != by_id[c["id"]]["state"] or build_questions(bank, fill=fill) != by_id[c["id"]]["questions"]:
            raise ValueError("per-case state or wording differs from annotation packet")
    kinds = Counter(v["kind"] for c in cases for v in c["provenance"].values())
    if provenance["AB_agreed_cells"] != kinds["AB_agreement"] or provenance["AI_arbitration_cells_adopted_by_user"] != kinds["user_adopted_final_arbitration"]:
        raise ValueError("reference provenance counts differ from cases")
    binding = {k: doc[k] for k in ("reference_version", "sample_sha256", "bank_sha256", "filled_questions_sha256", "model")}
    binding["reference_sha256"] = sha(reference)
    binding["evaluator_version"] = EVALUATOR_VERSION
    return bank, doc, binding


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        number = float(value)
    except (ValueError, TypeError):
        return False
    return math.isfinite(number) and 0 <= number <= 1


def candidate_for(path: Path, baseline):
    if not path.resolve().is_relative_to(CANDIDATE_ROOT.resolve()):
        raise ValueError("candidate must be an offline bank under banks/candidates")
    candidate = load_bank(path)
    if "candidate" not in candidate.version or candidate.version == baseline.version:
        raise ValueError("candidate needs its own explicitly experimental bank version")
    if (candidate.ids() != baseline.ids() or candidate.model != baseline.model
            or candidate.ambiguity != baseline.ambiguity or candidate.fill_defaults != baseline.fill_defaults):
        raise ValueError("candidate may not change model, labels/order, thresholds or fill defaults")
    changed = []
    for q, old in zip(candidate.questions, baseline.questions):
        if q.jtype != old.jtype or set(q.criteria) != set(old.criteria):
            raise ValueError("candidate must preserve question types and closed labels")
        for attr in ("scope", "evidence", "unclear_labels", "feeds", "on_ambiguous", "version", "aw_instruction"):
            if getattr(q, attr) != getattr(old, attr):
                raise ValueError("candidate may not change evidence, review routing or other operational fields")
        if q.instructions != old.instructions or q.criteria != old.criteria:
            changed.append(q.id)
    if not changed or not set(changed) <= {"speech_act", "detail_level"}:
        raise ValueError("candidate changes must be limited to speech_act/detail_level wording")
    return candidate, {"candidate_version": candidate.version, "candidate_sha256": candidate.sha256,
        "baseline_bank_sha256": baseline.sha256, "changed_questions": changed,
        "owner_definition_review_pending": True,
        "evaluation_scope": "development_regression_on_old_reference_not_independent_acceptance"}


class ProbabilityCheckedClient:
    """Validate all returned probabilities before interpret() discards the full choice map."""
    def __init__(self, client):
        self.client = client
        self.mock = bool(getattr(client, "mock", False))

    def call(self, body):
        response = self.client.call(body)
        if not isinstance(response, dict) or not isinstance(response.get("answers", {}), dict):
            raise ValueError("malformed answer response")
        answers = dict(response.get("answers", {}))
        for qid, question in body["questions"].items():
            answer = answers.get(qid)
            if answer is None:
                continue
            valid = isinstance(answer, dict)
            if valid and question["type"] == "choice":
                probs = answer.get("probabilities")
                valid = isinstance(probs, dict) and bool(probs) and all(
                    label in question["criteria"] and probability(p) for label, p in probs.items())
            elif valid:
                valid = probability(answer.get("noul"))
            if not valid:
                # Missing is already an invalid answer in interpret(); preserve other valid qids.
                answers.pop(qid, None)
        return {**response, "answers": answers}


def safe_item(item: dict | None, q) -> dict:
    """Keep only closed labels and numeric probabilities; never publish free-text model fields."""
    it = item or {}
    answer = it.get("answer")
    invalid = bool(it.get("invalid_reason")) or bool(it.get("invalid"))
    try:
        answer = normalize_answer(answer, q)
    except (ValueError, AttributeError, TypeError):
        invalid = True
    p = it.get("p")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
        invalid = True
    if q.jtype == "noul":
        py = it.get("p_yes")
        if isinstance(py, bool) or not isinstance(py, (int, float)) or not math.isfinite(py) or not 0 <= py <= 1:
            invalid = True
    if type(it.get("ambiguous")) is not bool:
        invalid = True
    if invalid:
        return {"answer": None, "p": None, "ambiguous": False, "invalid": True}
    out = {"answer": answer, "p": p, "ambiguous": it["ambiguous"], "invalid": False}
    if q.jtype == "noul":
        out["p_yes"] = it["p_yes"]
    return out


def validate_resume(path: Path | None, binding: dict, bank, ids: set) -> dict:
    if path is None:
        return {}
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("binding") != binding or previous.get("live") is not True:
        raise ValueError("resume reference/bank/model mismatch or non-live results")
    rows = previous.get("results")
    if not isinstance(rows, dict) or not set(rows) <= ids:
        raise ValueError("resume contains unknown case IDs")
    reusable = {}
    for sid, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError("malformed resume result")
        items = row.get("items", {})
        if row.get("status") != "ok" or not isinstance(items, dict) or set(items) != set(bank.ids()):
            continue
        if any(not isinstance(item, dict) for item in items.values()):
            raise ValueError("malformed resume items")
        clean = {q.id: safe_item(items[q.id], q) for q in bank.questions}
        if not any(it["invalid"] for it in clean.values()):
            reusable[sid] = {"status": "ok", "items": clean}
    return reusable


def summarize(bank, doc: dict, results: dict) -> dict:
    by_question = {}
    for q in bank.questions:
        confusion = Counter()
        hits = usable_hits = invalid = ambiguous = pending = 0
        for c in doc["cases"]:
            expected = normalize_answer(c["expect"][q.id], q)
            row = results.get(c["id"])
            pending += row is None
            it = safe_item((row or {}).get("items", {}).get(q.id), q)
            invalid += it["invalid"]
            ambiguous += it["ambiguous"]
            hit = not it["invalid"] and it["answer"] == expected
            hits += hit
            usable_hits += hit and not it["ambiguous"]
            confusion[(expected, it["answer"] if not it["invalid"] else INVALID)] += 1
        by_question[q.id] = {"total": 100, "answer_agreement": hits, "unambiguous_agreement": usable_hits,
            "invalid_or_missing": invalid, "ambiguous": ambiguous, "not_called": pending,
            "confusion": [{"reference": a, "model": b, "count": n} for (a, b), n in sorted(confusion.items())]}
    risks = {"endorsement_reference_cases": 0, "endorsement_unambiguous_nonendorsement": [],
             "endorsement_invalid_or_missing": [], "endorsement_ambiguous": [],
             "arranged_yes_reference_cases": 0, "arranged_unambiguous_false_negative": []}
    for c in doc["cases"]:
        items = results.get(c["id"], {}).get("items", {})
        if c["expect"]["speech_act"] in ("亲历背书", "旁观推荐"):
            risks["endorsement_reference_cases"] += 1
            it = safe_item(items.get("speech_act"), bank.by_id()["speech_act"])
            if it["invalid"]:
                risks["endorsement_invalid_or_missing"].append(c["id"])
            elif it["ambiguous"]:
                risks["endorsement_ambiguous"].append(c["id"])
            elif it["answer"] not in ("亲历背书", "旁观推荐"):
                risks["endorsement_unambiguous_nonendorsement"].append(c["id"])
        if normalize_answer(c["expect"]["arranged"], bank.by_id()["arranged"]) == "是":
            risks["arranged_yes_reference_cases"] += 1
            it = safe_item(items.get("arranged"), bank.by_id()["arranged"])
            if not it["invalid"] and not it["ambiguous"] and it["answer"] == "否":
                risks["arranged_unambiguous_false_negative"].append(c["id"])
    return {"count": 100, "cells": 700, "called_cases": len(results),
        "answer_agreement_cells": sum(x["answer_agreement"] for x in by_question.values()),
        "unambiguous_agreement_cells": sum(x["unambiguous_agreement"] for x in by_question.values()),
        "invalid_or_missing_cells": sum(x["invalid_or_missing"] for x in by_question.values()),
        "ambiguous_cells": sum(x["ambiguous"] for x in by_question.values()),
        "reference_provenance": doc["reference_provenance"],
        "metrics_kind": "model_agreement_with_final_reference_not_independent_human_accuracy",
        "accepted": False, "release_accepted": False, "per_question": by_question,
        "reader_bank_risk_signals": risks}


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def evaluate(reference: Path, manifest: Path, questions: Path, output: Path, *, workers=4, resume=None, client=None, candidate=None):
    target = output.resolve()
    if target.is_relative_to(ROOT.resolve()) or any((p / ".git").is_file() or (p / ".git/HEAD").is_file() for p in (target, *target.parents)):
        raise ValueError("evaluation artifacts must remain outside any Git repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output is not empty; use a new directory and --resume")
    if not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    bank, doc, binding = load_reference(reference, manifest, questions)
    experiment = None
    if candidate is not None:
        bank, experiment = candidate_for(candidate, bank)
        if any(unfilled(bank, c["fill"]) for c in doc["cases"]):
            raise ValueError("candidate has unfilled per-case placeholders")
        binding = {**binding, "reference_bank_sha256": binding["bank_sha256"],
                   "bank_sha256": bank.sha256, "candidate_experiment": experiment}
    results = validate_resume(resume, binding, bank, {c["id"] for c in doc["cases"]})
    output.mkdir(parents=True, exist_ok=True)
    pending = [c for c in doc["cases"] if c["id"] not in results]
    if pending:
        try:
            client = ProbabilityCheckedClient(client if client is not None else JevClient())
        except JevError:
            save_json(output / "status.json", {"status": "blocked_missing_jev_credentials", "binding": binding,
                "live": False, "model_calls": 0, "release_accepted": False})
            return 4
    live = not bool(getattr(client, "mock", False))
    if results and not live:
        raise ValueError("cannot combine live checkpoints with a non-live client")

    def run(case):
        try:
            r = judge_state(client, bank, case["id"], case["state"], fill=case["fill"])
            items = {q.id: safe_item(r.items.get(q.id), q) for q in bank.questions}
            return {"status": "ok" if not any(it["invalid"] for it in items.values()) else "invalid", "items": items}
        except Exception as exc:
            # HTTP/model exceptions may echo prompts or secrets; keep a controlled error class only.
            return {"status": "error", "error_kind": "jev_call_failed" if isinstance(exc, JevError) else "invalid_response", "items": {}}

    def checkpoint():
        save_json(output / "checkpoint.json", {"binding": binding, "live": live, "results": results})

    checkpoint()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, c): c["id"] for c in pending}
        for f in cf.as_completed(futures):
            results[futures[f]] = f.result()
            checkpoint()
            if len(results) % 25 == 0:
                print(f"comment reference evaluation: {len(results)}/100 cases saved", flush=True)
    summary = summarize(bank, doc, results)
    if experiment is not None:
        summary["candidate_experiment"] = experiment
        summary["metrics_kind"] = "development_regression_with_frozen_old_reference_not_candidate_acceptance"
    summary.update({"binding": binding, "live": live, "status": "evaluation_complete" if all(r["status"] == "ok" for r in results.values()) else "evaluation_incomplete"})
    save_json(output / "summary.json", summary)
    lines = [f"# 模型对照 · {doc['reference_version']} · {bank.version} · {bank.model}", "",
        "参考标签包含用户明确采用的AI来源仲裁；结果表示与该参考的一致性，不能称为独立真人准确率。",
        "非live结果不用于模型验收。统计固定100条/700格，缺答与无效不能从分母删除。",
        "", "| 题 | 答案一致 / 100 | 无歧义一致 | 缺答/无效 | 歧义 |", "|---|---|---|---|---|"]
    if experiment is not None:
        lines[1:1] = ["", "候选定义尚待owner审阅；旧100条仅用于开发回归，不能据此宣称候选题库独立验收通过。"]
    for qid, m in summary["per_question"].items():
        lines.append(f"| {qid} | {m['answer_agreement']} | {m['unambiguous_agreement']} | {m['invalid_or_missing']} | {m['ambiguous']} |")
    lines += ["", f"答案一致 {summary['answer_agreement_cells']}/700；无歧义一致 {summary['unambiguous_agreement_cells']}/700。",
        "逐题混淆与读者题库风险信号见summary.json；风险信号不等于完整API放行行为。",
        "本报告不生成验收许可、不写账本、不修改评论开关。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    if summary["status"] != "evaluation_complete":
        return 1
    return 0 if summary["answer_agreement_cells"] == 700 else 3


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "manifest", "questions", "output"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--resume", type=Path)
    ap.add_argument("--candidate-bank", type=Path, help="offline wording experiment; never changes runtime bank")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    try:
        return evaluate(args.reference, args.manifest, args.questions, args.output,
                        workers=args.workers, resume=args.resume, candidate=args.candidate_bank)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        # Input validation errors are controlled messages, not source text dumps.
        print(f"invalid evaluation input: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
