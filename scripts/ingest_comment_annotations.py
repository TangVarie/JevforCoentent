#!/usr/bin/env python3
"""Validate two human sheets against the fixed sample and export an unadjudicated review packet.

No model calls, no automatic arbitration, no acceptance record, no database writes.
Raw source texts and annotation artifacts must remain outside tracked repository files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import build_questions, load_bank
from judge.comments import comment_state, fill_for
from scripts.backfill_comments import post_for
from scripts.prepare_comment_gold import SAMPLE, spreadsheet_cell

ROOT = Path(__file__).resolve().parent.parent
CONTEXT = ("comment_id", "note_id", "category", "角色", "帖子标题", "帖子正文", "帖子要点", "评论原文")
NOTE = "不确定或分歧备注"
BOOL_ALIASES = {"是": "是", "否": "否", "true": "是", "false": "否"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_answer(value: str, question) -> str:
    value = value.strip()
    if question.jtype == "noul":
        result = BOOL_ALIASES.get(value.casefold())
    else:
        result = value if value in question.criteria else None
    if result is None:
        raise ValueError(f"{question.id}: invalid/blank answer {value!r}")
    return result


def expected_context(c: dict) -> dict:
    post = post_for(c)
    fill = fill_for(post)
    st = comment_state(post, c["comment_text"], role=c.get("comment_role") or "未知")
    return {"comment_id": c["comment_id"], "note_id": c["note_id"], "category": c["category"],
            "角色": c["comment_role"], "帖子标题": st["帖子标题"], "帖子正文": st["帖子正文"],
            "帖子要点": fill["post_points"], "评论原文": c["comment_text"]}


def read_sheet(path: Path, expected: dict, bank):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError(f"{path.name}: duplicate headers")
        required = {*CONTEXT, *bank.ids(), NOTE}
        if not required <= set(fields):
            raise ValueError(f"{path.name}: missing columns {sorted(required - set(fields))}")
        rows = list(reader)
    by_id, conversions = {}, []
    for row in rows:
        sid = row.get("comment_id")
        if None in row or sid in by_id or sid not in expected:
            raise ValueError(f"{path.name}: malformed row, duplicate or unknown comment_id {sid!r}")
        original = expected[sid]
        for key in CONTEXT:
            if row.get(key) != str(spreadsheet_cell(original[key])):
                raise ValueError(f"{path.name}: source context changed ({sid}, {key})")
        answers = {}
        for q in bank.questions:
            value = normalize_answer(row.get(q.id) or "", q)
            answers[q.id] = value
            if value != row[q.id]:
                conversions.append({"comment_id": sid, "question_id": q.id, "from": row[q.id], "to": value})
        by_id[sid] = {"answers": answers, "note": row.get(NOTE) or ""}
    if set(by_id) != set(expected):
        raise ValueError(f"{path.name}: missing {len(set(expected) - set(by_id))} comments")
    return by_id, conversions


def agreement(a: list[str], b: list[str]) -> dict:
    n = len(a)
    if n != len(b) or not n:
        raise ValueError("agreement needs equal nonempty paired answers")
    ca, cb = Counter(a), Counter(b)
    same = sum(x == y for x, y in zip(a, b))
    chance = sum(ca[k] * cb[k] for k in ca) / (n * n)
    return {"count": n, "agree": same, "disagree": n - same, "agreement": same / n,
            "kappa": (same / n - chance) / (1 - chance) if chance < 1 else None,
            "A_counts": dict(ca), "B_counts": dict(cb),
            "pairs": [{"A": x, "B": y, "count": count} for (x, y), count in sorted(Counter(zip(a, b)).items())]}


def sample_issue(c: dict) -> str | None:
    # These are source-quality checks only. Notes never act as executable instructions.
    text = (c.get("comment_text") or "").strip()
    if re.fullmatch(r"评论\s*[A-Za-z0-9一二三]*\s*[:：]?", text) or text in {"【素人科普评论】", "【素人评论】", "【评论】"}:
        return "只有评论位标签/空占位，没有实质评论；建议替换并由A/B补标"
    return None


def write_csv(path: Path, fields: list[str], rows: list[dict]):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fields)
        writer.writeheader()
        writer.writerows({k: spreadsheet_cell(v) for k, v in row.items()} for row in rows)


def ingest(a_path: Path, b_path: Path, sample: Path, output: Path, *, manifest: Path) -> dict:
    # Prevent an accidental git add from publishing source text or human annotation data.
    target = output.resolve()
    # Managed sandboxes can expose empty .git permission sentinels outside actual checkouts.
    inside_git = any((p / ".git").is_file() or (p / ".git" / "HEAD").is_file() for p in (target, *target.parents))
    if target.is_relative_to(ROOT.resolve()) or inside_git:
        raise ValueError("output must be outside any Git repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output is not empty; do not overwrite human arbitration work")
    doc = json.loads(sample.read_text(encoding="utf-8"))
    cases = doc["notes"]
    if len(cases) != 100 or len({c["comment_id"] for c in cases}) != 100:
        raise ValueError("fixed sample must have exactly 100 unique comments")
    bank = load_bank(ROOT / "banks/comment_reader_v0.4.yaml")
    source_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    if (source_manifest.get("sample_sha256") != digest(sample)
            or source_manifest.get("sample_version") != doc["sample_version"]
            or source_manifest.get("count") != 100 or source_manifest.get("bank_sha256") != bank.sha256):
        raise ValueError("original packet manifest does not match sample/current bank; do not mix versions")
    expected = {c["comment_id"]: expected_context(c) for c in cases}
    a, ac = read_sheet(a_path, expected, bank)
    b, bc = read_sheet(b_path, expected, bank)
    metrics, conflicts, consensus, reviews = {}, [], [], []
    for q in bank.questions:
        metrics[q.id] = agreement([a[s]["answers"][q.id] for s in expected], [b[s]["answers"][q.id] for s in expected])
    for c in cases:
        sid = c["comment_id"]
        filled = build_questions(bank, fill=fill_for(post_for(c)))
        agreed, disputed = {}, []
        for q in bank.questions:
            av, bv = a[sid]["answers"][q.id], b[sid]["answers"][q.id]
            if av == bv:
                agreed[q.id] = av
            else:
                disputed.append(q.id)
                conflicts.append({**expected[sid], "question_id": q.id, "题干": filled[q.id]["instructions"],
                                  "选项定义": json.dumps(filled[q.id]["criteria"], ensure_ascii=False),
                                  "A答案": av, "B答案": bv, "A备注": a[sid]["note"], "B备注": b[sid]["note"],
                                  "仲裁答案": "", "仲裁人": "", "仲裁理由": ""})
        issue = sample_issue(c)
        if issue:
            reviews.append({**expected[sid], "问题": issue, "A备注": a[sid]["note"], "B备注": b[sid]["note"],
                            "处理决定": "", "处理人": "", "理由": ""})
        consensus.append({"comment_id": sid, "answers": agreed, "disputed_questions": disputed,
                          "sample_issue": issue, "A_note": a[sid]["note"], "B_note": b[sid]["note"]})
    summary = {"sample_version": doc["sample_version"], "sample_sha256": digest(sample),
               "bank": bank.name, "bank_version": bank.version, "bank_sha256": bank.sha256, "model": bank.model,
               "count": len(cases), "annotation_cells_each": len(cases) * len(bank.questions),
               "agreed_cells": sum(m["agree"] for m in metrics.values()), "disputed_cells": len(conflicts),
               "disputed_comments": sum(bool(c["disputed_questions"]) for c in consensus),
               "fully_agreed_comments": sum(not c["disputed_questions"] for c in consensus),
               "source_review_comments": len(reviews), "gold_ready": False, "accepted": False,
               "status": "awaiting_human_arbitration_and_model_evaluation",
               "metrics_kind": "inter_annotator_agreement_not_model_accuracy",
               "source_files": {"A": {"sha256": digest(a_path), "normalizations": len(ac)},
                                "B": {"sha256": digest(b_path), "normalizations": len(bc)}},
               "per_question": metrics}
    output.mkdir(parents=True, exist_ok=True)
    conflict_fields = list(CONTEXT) + ["question_id", "题干", "选项定义", "A答案", "B答案", "A备注", "B备注", "仲裁答案", "仲裁人", "仲裁理由"]
    write_csv(output / "分歧仲裁.csv", conflict_fields, conflicts)
    review_fields = list(CONTEXT) + ["问题", "A备注", "B备注", "处理决定", "处理人", "理由"]
    write_csv(output / "样本复核.csv", review_fields, reviews)
    for name, data in (("summary.json", summary), ("共识待仲裁.json", consensus),
                       ("格式规范化记录.json", {"A": ac, "B": bc})):
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# J-03 · 两人标注对账与仲裁", "", "两份表各100条、每条7题；comment_id集合与全部上下文均与原始固定样本一致。",
             "true/false仅规范为是/否，未改任何实质标注；原文件不改。共识只记录相同答案，不作为完整金标准或上线验收。", "",
             f"700格中{summary['agreed_cells']}格一致，{len(conflicts)}格分歧，涉及{summary['disputed_comments']}条评论；完整一致{summary['fully_agreed_comments']}条。",
             f"另有{len(reviews)}条空占位需来源复核，建议换成同品类实质评论，并由A/B独立补标。", "",
             "| 题 | 一致 / 100 | 一致率 | Cohen κ |", "|---|---|---|---|"]
    for qid, m in metrics.items():
        k = "不可估计（答案无变异）" if m["kappa"] is None else f"{m['kappa']:.3f}"
        lines.append(f"| {qid} | {m['agree']} | {m['agreement']:.0%} | {k} |")
    lines += ["", "以上是两人一致性，尚不是Jev命中率。安排第三位/owner对照原定义仲裁，不能用Jev答案替人类裁决。",
              "分歧仲裁.csv一行一格，已附原始帖子/评论、按篇题面、A/B答案与备注；只填写仲裁答案、仲裁人、仲裁理由。",
              "若定义需改，升题库版本、重新标注和验收，不把现有分歧静默批量改成某一方。",
              "样本复核.csv单独处理空占位；替换时保留原样本/标注与新样本的完整版本关系，不能用98条当100条。",
              "共识待仲裁.json只有已一致的格，未决格仍为空缺。模型评估、危险漏报检查和owner验收尚未做；不开评论开关、不写primary账本。",
              "CSV均转义公式起始符；判定上下文的JSON原文保持原样。", ""]
    (output / "对账说明.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True)
    ap.add_argument("--b", type=Path, required=True)
    ap.add_argument("--sample", type=Path, default=SAMPLE)
    ap.add_argument("--manifest", type=Path, required=True, help="original blank packet's manifest.json")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        result = ingest(args.a, args.b, args.sample, args.output, manifest=args.manifest)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    print(json.dumps({k: result[k] for k in ("count", "agreed_cells", "disputed_cells", "disputed_comments", "source_review_comments", "gold_ready")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
