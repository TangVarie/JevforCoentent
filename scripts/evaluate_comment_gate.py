#!/usr/bin/env python3
"""D-112 gate round: C4 unchanged; four gating questions decide pass/fail, three advisory questions are only reported.

Raw comment/post text never enters the repository or the artifacts: the sample is re-read from production
by comment_id and must reproduce the frozen sha256 byte for byte; outputs carry ids, closed labels and
probabilities only (``safe_item``).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.comments import fill_for
from judge.core import judge_state
from judge.jev_client import JevClient, JevError
from scripts import evaluate_comment_candidate as C
from scripts import evaluate_comment_reference as E
from scripts.backfill_comments import post_for, state_reader

CONTRACT = E.ROOT / "config/comment_gate_d112_contract.json"
VERSION = "comment-gate-d112-v1"
ENDORSE = ("亲历背书", "旁观推荐")
SAMPLE_KEYS = ("comment_id", "note_id", "comment_text", "comment_role", "title", "raw_content", "project_id", "category")


def load_contract(path: Path = None) -> dict:
    contract = json.loads((path or CONTRACT).read_text(encoding="utf-8"))
    bank = C.candidate()
    if (contract.get("decision") != "D-112" or contract["candidate_version"] != bank.version
            or contract["bank_sha256"] != bank.sha256 or contract["model"] != bank.model):
        raise ValueError("contract does not bind the unchanged C4 bank")
    gating, advisory = contract["gating_questions"], contract["advisory_questions"]
    if sorted(gating + advisory) != sorted(bank.ids()) or set(gating) & set(advisory):
        raise ValueError("gating/advisory split must cover the seven questions exactly once")
    return contract


def canonical_sample_bytes(rows: list) -> bytes:
    return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")


def verify_sample(rows: list, contract: dict) -> list:
    spec = contract["sample"]
    if [r.get("comment_id") for r in rows] != spec["ids"] or len(rows) != spec["count"]:
        raise ValueError("sample ids/order differ from the frozen sample")
    for r in rows:
        if list(r) != list(SAMPLE_KEYS):
            raise ValueError("sample row shape differs")
        md5 = [hashlib.md5(r["comment_text"].encode()).hexdigest(), hashlib.md5(r["raw_content"].encode()).hexdigest()]
        if md5 != spec["md5"][r["comment_id"]]:
            raise ValueError(f"sample text drifted for {r['comment_id']}")
    if hashlib.sha256(canonical_sample_bytes(rows)).hexdigest() != spec["sha256"]:
        raise ValueError("sample sha256 differs from the frozen sample")
    return rows


def fetch_sample(contract: dict, *, url: str, key: str, get=None) -> list:
    """Read the 40 rows from production (truth_vault, read-only) and rebuild the frozen sample exactly."""
    if get is None:
        import requests
        def get(path, params):
            r = requests.get(f"{url.rstrip('/')}/rest/v1/{path}", params=params, timeout=60, headers={
                "apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
            r.raise_for_status()
            return r.json()
    ids = contract["sample"]["ids"]
    comments = {r["comment_id"]: r for r in get("comments", {
        "select": "comment_id,note_id,content,comment_role", "comment_id": f"in.({','.join(ids)})"})}
    notes_ids = sorted({c["note_id"] for c in comments.values()})
    notes = {r["note_id"]: r for r in get("notes", {
        "select": "note_id,title,raw_content,project_id", "note_id": f"in.({','.join(notes_ids)})"})}
    proj_ids = sorted({n["project_id"] for n in notes.values()})
    projects = {r["project_id"]: r for r in get("projects", {
        "select": "project_id,category", "project_id": f"in.({','.join(proj_ids)})"})}
    n_chars = contract["sample"]["raw_content_prefix_chars"]
    rows = []
    for cid in ids:
        c = comments[cid]; n = notes[c["note_id"]]
        rows.append({"comment_id": cid, "note_id": c["note_id"], "comment_text": c["content"],
                     "comment_role": c["comment_role"], "title": n["title"],
                     "raw_content": (n["raw_content"] or "")[:n_chars], "project_id": n["project_id"],
                     "category": projects[n["project_id"]]["category"]})
    return verify_sample(rows, contract)


def load_reference(path: Path, contract: dict, rows: list, bank) -> dict:
    ref = contract["reference"]
    if ref.get("frozen") is not True or not ref.get("sha256"):
        raise ValueError("reference not frozen in the contract yet: no model call before the human reference is fixed")
    if E.sha(path) != ref["sha256"]:
        raise ValueError("reference file differs from the frozen sha256")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("source_kind") != ref["source_kind"] or doc.get("ai_suggestions_shown") is not False:
        raise ValueError("reference provenance invalid")
    cases = doc.get("cases")
    if [c.get("id") for c in cases] != [r["comment_id"] for r in rows]:
        raise ValueError("reference ids/order differ from sample")
    by_id = bank.by_id()
    for c in cases:
        if set(c.get("expect", {})) != set(bank.ids()):
            raise ValueError("each case needs all seven keys (null = unresolved)")
        for qid, v in c["expect"].items():
            q = by_id[qid]
            allowed = ["是", "否"] if q.jtype == "noul" else list(q.criteria)
            if v is not None and v not in allowed:
                raise ValueError("reference label outside closed vocabulary")
    return doc


def score(bank, contract: dict, doc: dict, results: dict) -> dict:
    by_id = bank.by_id()
    th = contract["thresholds"]
    per_q, unresolved_gating = {}, 0
    for q in bank.questions:
        hit = amb = invalid = resolved = 0
        for c in doc["cases"]:
            expected = c["expect"][q.id]
            if expected is None:
                unresolved_gating += q.id in contract["gating_questions"]
                continue
            resolved += 1
            it = E.safe_item((results.get(c["id"]) or {}).get("items", {}).get(q.id), q)
            invalid += it["invalid"]; amb += it["ambiguous"]
            hit += (not it["invalid"]) and it["answer"] == E.normalize_answer(expected, q)
        per_q[q.id] = {"resolved": resolved, "agreement": hit, "ambiguous": amb, "invalid_or_missing": invalid,
                       "role": "gating" if q.id in contract["gating_questions"] else "advisory"}
    g = [per_q[q] for q in contract["gating_questions"]]
    g_res = sum(x["resolved"] for x in g); g_hit = sum(x["agreement"] for x in g); g_amb = sum(x["ambiguous"] for x in g)
    fn = []
    for c in doc["cases"]:
        if c["expect"]["speech_act"] in ENDORSE:
            it = E.safe_item((results.get(c["id"]) or {}).get("items", {}).get("speech_act"), by_id["speech_act"])
            if not it["invalid"] and not it["ambiguous"] and it["answer"] not in ENDORSE:
                fn.append(c["id"])
    reasons = []
    if any(x["invalid_or_missing"] for x in g):
        reasons.append("invalid_or_missing")
    if unresolved_gating > th["reference_unresolved_gating_cells_max"]:
        reasons.append("reference_incomplete")
    if not g_res or g_hit / g_res < th["gating_agreement_min"]:
        reasons.append("gating_agreement")
    if any(not x["resolved"] or x["agreement"] / x["resolved"] < th["gating_per_question_min"] for x in g):
        reasons.append("gating_per_question")
    if not g_res or g_amb / g_res > th["gating_ambiguous_fraction_max"]:
        reasons.append("gating_ambiguity")
    if len(fn) > th["unambiguous_endorsement_false_negatives_max"]:
        reasons.append("endorsement_false_negative")
    return {"per_question": per_q, "gating": {"resolved_cells": g_res, "agreement": g_hit, "ambiguous": g_amb,
            "unresolved_reference_cells": unresolved_gating},
            "unambiguous_endorsement_false_negatives": fn,
            "gate": {"passed": not reasons, "reasons": reasons, "thresholds": th}}


def run(contract: dict, rows: list, doc: dict, output: Path, *, client=None, workers: int = 4) -> int:
    bank = C.candidate()
    output.mkdir(parents=True, exist_ok=True)
    if client is None:
        try:
            client = E.ProbabilityCheckedClient(JevClient(), require_all_choice_labels=True)
        except JevError:
            E.save_json(output / "status.json", {"status": "blocked_missing_credentials", "model_calls": 0})
            return 4
    source = {r["comment_id"]: r for r in rows}

    def one(cid):
        row = source[cid]
        try:
            judged = judge_state(client, bank, cid, state_reader(row), fill=fill_for(post_for(row)))
            items = {q.id: E.safe_item(judged.items.get(q.id), q) for q in bank.questions}
            return {"status": "invalid" if any(i["invalid"] for i in items.values()) else "ok", "items": items}
        except Exception as exc:  # never echo private text or credentials
            return {"status": "error", "error_kind": "jev_call_failed" if isinstance(exc, JevError) else "invalid_response", "items": {}}

    results, calls = {}, 0
    for attempt in range(2):  # second pass only re-asks failed cases; valid results are never re-asked
        pending = [cid for cid in contract["sample"]["ids"] if results.get(cid, {}).get("status") != "ok"]
        if not pending:
            break
        calls += len(pending)
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for cid, res in zip(pending, pool.map(one, pending)):
                results[cid] = res
    summary = score(bank, contract, doc, results)
    complete = all(results.get(cid, {}).get("status") == "ok" for cid in contract["sample"]["ids"])
    summary.update(decision="D-112", evaluator_version=VERSION, sample_sha256=contract["sample"]["sha256"],
                   reference_sha256=contract["reference"]["sha256"], bank_sha256=bank.sha256, model=bank.model,
                   live=not bool(getattr(client, "mock", False)), model_calls=calls,
                   status="evaluation_complete" if complete else "evaluation_incomplete",
                   reference_source=contract["reference"]["source_kind"], release_accepted=False)
    E.save_json(output / "answers.json", {"decision": "D-112", "results": results})
    E.save_json(output / "summary.json", summary)
    return 1 if not complete else 0 if summary["gate"]["passed"] else 3


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["validate", "run"])
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args(argv)
    try:
        contract = load_contract()
        if (E.ROOT / contract["result_path"]).exists():
            raise ValueError("this batch already has a committed result; max_live_runs=1")
        url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise ValueError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY needed to re-read the frozen sample")
        rows = fetch_sample(contract, url=url, key=key)
        a.output.mkdir(parents=True, exist_ok=True)
        if a.mode == "validate":
            ref_ok = contract["reference"]["frozen"] is True
            if ref_ok:
                load_reference(E.ROOT / contract["reference"]["path"], contract, rows, C.candidate())
            E.save_json(a.output / "status.json", {"status": "sample_verified", "sample_sha256": contract["sample"]["sha256"],
                        "reference_frozen": ref_ok, "model_calls": 0})
            print(f"sample verified ({len(rows)} rows, sha256 {contract['sample']['sha256'][:12]}); reference frozen: {ref_ok}")
            return 0
        doc = load_reference(E.ROOT / contract["reference"]["path"], contract, rows, C.candidate())
        return run(contract, rows, doc, a.output, workers=a.workers)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"D-112 gate evaluation refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
