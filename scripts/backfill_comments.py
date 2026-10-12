#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评论回填：给 truth_vault.comments 跑两套题库，出账本 SQL 和每篇的「评论构成」。

  · 运营侧 banks/comment_ops_v0.2.yaml → comment_intent / is_scripted（表里现有六值闭集；state 不带蓝词清单，见题库头注释）
  · 读者侧 banks/comment_reader_v0.4.yaml → 言语行为 / 点名品牌 / 接住帖子 / 细节 / 语域 / 读者用处 / 像不像安排的

  python3 scripts/backfill_comments.py --from-db --limit 500 --sql comments.sql --summary summary.csv [--bank both|ops|reader]
  python3 scripts/backfill_comments.py --input comments.json ...      # [{comment_id, note_id, comment_text, comment_role, title, raw_content}]

账本：subject_type = 'comment'（要先跑 migrations/notes_v1_17_judge_subjects.sql），subject_id = comments.comment_id。
两套题库分别落两批行（question_id 不同，不冲突）。默认 shadow；primary 写库需 --acceptance 指向人工验收记录。
答案只落账本，不回写 comments 表的 comment_intent / comment_type / is_scripted 三列（docs/31 §3 ② 的决定：
业务表归 TV 的同步脚本管，judge 不改 TV 业务表；要按列看就从账本 join）。
只读 comments / notes；写库用 --write（需要 SUPABASE_SERVICE_ROLE_KEY），否则只出 SQL。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import load_bank  # noqa: E402
from judge.core import judge_state, ledger_rows, postgrest_upsert, rows_to_sql  # noqa: E402
from judge.jev_client import JevClient, JevError  # noqa: E402
from judge.comments import comment_state, fill_for  # noqa: E402
from judge.spans import build_spans  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "banks" / "comment_reader_v0.4.yaml"
OPS = ROOT / "banks" / "comment_ops_v0.2.yaml"


def _pg(url, key, path):
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


PAGE = 1000   # PostgREST 的 db-max-rows 默认 1000: 单次 GET 带 limit=9652 也只回 1000 行, 不报错


def _pg_pages(url, key, base_query: str, limit: int, page: int = PAGE) -> list:
    """按 offset 翻页直到空页或够 limit。以前一次 GET 带 limit=N 就当拿全了 —— 服务端会静默钳到
    db-max-rows (默认 1000), 9,652 条评论 "回填完" 实际只回填 10% (2026-10-08 TV 审计 B-09)。"""
    out: list = []
    offset = 0
    while len(out) < limit:
        want = min(page, limit - len(out))
        rows = _pg(url, key, f"{base_query}&limit={want}&offset={offset}")
        if not rows:
            break
        out.extend(rows)
        offset += len(rows)
        # A short page may be the server's configured cap; only an empty page proves the end.
    return out


def fetch(limit: int, project: str | None):
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("--from-db 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    q = "comments?select=comment_id,note_id,content,comment_role,is_pinned&order=note_id,comment_order,comment_id"
    if project:
        q += f"&note_id=like.{urllib.parse.quote(project)}*"
    comments = _pg_pages(url, key, q, limit)
    ids = sorted({c["note_id"] for c in comments})
    notes = {}
    for i in range(0, len(ids), 150):
        chunk = ",".join(urllib.parse.quote(x) for x in ids[i:i + 150])
        for n in _pg(url, key, f"notes?select=note_id,title,raw_content,project_id,projects(category)&note_id=in.({chunk})"):
            notes[n["note_id"]] = n
    for c in comments:
        n = notes.get(c["note_id"], {})
        c["comment_text"] = c.pop("content", None) or ""
        c["title"] = n.get("title"); c["raw_content"] = n.get("raw_content") or ""
        c["project_id"] = n.get("project_id")
        c["category"] = (n.get("projects") or {}).get("category")
    return comments


def load_input(path: str) -> list:
    """本地文件两种形状：[{comment_id, note_id, comment_text, comment_role, title, raw_content}]，
    或 {"notes": {note_id: raw_content}, "comments": [...]}（正文只写一次）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("notes"), list):
        return data["notes"]
    if isinstance(data, dict) and "comments" in data:
        notes = data.get("notes") or {}
        for c in data["comments"]:
            c.setdefault("raw_content", notes.get(c["note_id"], ""))
            c.setdefault("title", None)
        return data["comments"]
    return data


def head_of(raw: str, n: int = 300) -> str:
    return (raw or "").strip()[:n]


def post_for(c: dict) -> dict:
    spans = build_spans(c.get("raw_content") or "", mode=c.get("title_extraction") or "markers", title_col=c.get("title"))
    # Keep the question's post_points in the same text window that the judge/annotator sees.
    return {"title": spans.get("title") or c.get("title") or "", "body": (spans.get("body") or "")[:600],
            "kind": c.get("kind") or ("agency" if c.get("category") == "教育"
                                      or str(c.get("note_id", "")).startswith(("TUGE", "TGV")) else "product")}


def state_reader(c: dict) -> dict:
    return comment_state(post_for(c), c.get("comment_text") or "", role=c.get("comment_role") or "未知")


def state_ops(c: dict) -> dict:
    st = state_reader(c)
    # 不给蓝词清单（co-v0.2）：那是项目 / 品牌层信息（D-079、Mode A）；评论里有没有目标蓝词由 TV 的 contains_blue_keyword 代码列管
    return st


def validate_acceptance(path: str | None, banks: list):
    """Consistency check of the owner's human acceptance record, not a substitute for annotation."""
    if not path:
        raise ValueError("primary 写库需先完成 J-03 两人独立标注、仲裁和模型评估，并提供 --acceptance")
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    reviewers = record.get("independent_annotators", [])
    if (record.get("accepted") is not True or record.get("sample_count", 0) < 100
            or len(set(reviewers)) < 2 or not record.get("arbitration_artifact")
            or not record.get("evaluation_artifact") or not record.get("accepted_by")):
        raise ValueError("评论人工验收记录不完整")
    for bank, _ in banks:
        if record.get("bank_sha256", {}).get(bank.name) != bank.sha256:
            raise ValueError(f"验收题库摘要不匹配: {bank.name}")


def complete(rows: list, comment_id: str, bank, run_tag: str) -> bool:
    got = {r["question_id"] for r in rows if r.get("subject_id") == comment_id
           and r.get("subject_type") == "comment" and r.get("bank_sha256") == bank.sha256
           and r.get("run_tag") == run_tag and r.get("extractor") == "jev:" + bank.model.replace("jev-", "")
           and r.get("invalid_reason") is None and r.get("answer") is not None}
    return set(bank.by_id()) <= got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-db", action="store_true"); ap.add_argument("--input"); ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--project"); ap.add_argument("--bank", choices=["both", "ops", "reader"], default="both")
    ap.add_argument("--run-tag", default="shadow-comments"); ap.add_argument("--sql"); ap.add_argument("--summary"); ap.add_argument("--raw")
    ap.add_argument("--rows-json"); ap.add_argument("--resume", help="完整有效的同题库/模型/run_tag 账本行才跳过")
    ap.add_argument("--input-snapshot", help="保存本批文本输入，续跑应使用这个固定快照")
    ap.add_argument("--acceptance", help="primary 写库前的 owner 人工验收记录 JSON")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--workers", type=int, default=4); ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    if args.from_db == bool(args.input):
        ap.error("必须且只能指定 --from-db 或 --input")
    if not 1 <= args.limit <= 30000 or not 1 <= args.workers <= 8:
        ap.error("limit 需为 1..30000，workers 需为 1..8")
    if args.mock and args.write:
        ap.error("mock 答案不允许写库")
    banks = []
    if args.bank in ("both", "ops"):
        banks.append((load_bank(OPS, name="comment_ops_v0.2"), state_ops))
    if args.bank in ("both", "reader"):
        banks.append((load_bank(READER, name="comment_reader_v0.4"), state_reader))
    if args.write and not args.run_tag.startswith("shadow-"):
        try:
            validate_acceptance(args.acceptance, banks)
        except (ValueError, OSError) as exc:
            ap.error(str(exc))
    comments = fetch(args.limit, args.project) if args.from_db else load_input(args.input)[:args.limit]
    if len({c["comment_id"] for c in comments}) != len(comments):
        ap.error("输入包含重复 comment_id")
    if args.input_snapshot:
        Path(args.input_snapshot).write_text(json.dumps(comments, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        client = JevClient(mock=args.mock)
    except JevError as exc:
        sys.exit(str(exc))

    previous = json.loads(Path(args.resume).read_text(encoding="utf-8")) if args.resume else []
    note_ids = {c["comment_id"]: c["note_id"] for c in comments}
    bank_by_sha = {bank.sha256: bank for bank, _ in banks}
    resumed = defaultdict(list)
    rows_by_key = {}
    for row in previous:
        bank = bank_by_sha.get(row.get("bank_sha256"))
        if (bank is None or row.get("subject_type") != "comment" or row.get("subject_id") not in note_ids
                or row.get("run_tag") != args.run_tag
                or row.get("question_id") not in bank.by_id()
                or row.get("extractor") != "jev:" + bank.model.replace("jev-", "")):
            continue
        key = (row["subject_id"], bank.sha256, row["question_id"])
        rows_by_key[key] = row
    for (sid, sha, _), row in rows_by_key.items():
        resumed[sid, sha].append(row)
    raw, errors, per_note = [], [], defaultdict(Counter)

    def checkpoint():
        if args.rows_json:
            tmp = Path(args.rows_json + ".tmp")
            tmp.write_text(json.dumps(list(rows_by_key.values()), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(args.rows_json)

    def one(c):
        out = []
        for bank, mk in banks:
            if not args.mock and complete(resumed[c["comment_id"], bank.sha256], c["comment_id"], bank, args.run_tag):
                continue
            try:
                r = judge_state(client, bank, c["comment_id"], mk(c), subject_type="comment", fill=fill_for(post_for(c)))
            except (JevError, ValueError) as exc:
                out.append((bank, str(exc)))
                continue
            out.append((bank, r))
        return c, out

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for index, (c, outs) in enumerate(ex.map(one, comments), 1):
            for bank, r in outs:
                if isinstance(r, str):
                    errors.append({"comment_id": c["comment_id"], "bank": bank.name, "error": r})
                    continue
                # Retried invalid/partial bank rows are replaced, never duplicated in checkpoints.
                for row in ledger_rows(r, bank, run_tag=args.run_tag):
                    rows_by_key[c["comment_id"], bank.sha256, row["question_id"]] = row
                raw.append({"comment_id": c["comment_id"], "note_id": c["note_id"], "bank": bank.name, "items": r.items})
                if any(it.get("invalid_reason") for it in r.items.values()):
                    errors.append({"comment_id": c["comment_id"], "bank": bank.name, "error": "缺答/选项外，续跑重试"})
            if index % 25 == 0:
                checkpoint()
    checkpoint()
    rows = list(rows_by_key.values())
    for row in rows:
        if row.get("subject_id") not in note_ids or row.get("run_tag") != args.run_tag or not row.get("answer"):
            continue
        if row["question_id"] in ("speech_act", "comment_intent"):
            prefix = "运营:" if row["question_id"] == "comment_intent" else ""
            per_note[note_ids[row["subject_id"]]][prefix + str(row["answer"])] += 1
    if args.sql:
        Path(args.sql).write_text(rows_to_sql(rows), encoding="utf-8")
    if args.raw:
        Path(args.raw).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        Path(args.raw + ".errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.summary:
        keys = sorted({k for c in per_note.values() for k in c})
        with open(args.summary, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["note_id", "comments"] + keys)
            for nid, cnt in per_note.items():
                w.writerow([nid, sum(v for k, v in cnt.items() if not k.startswith("运营:"))] + [cnt.get(k, 0) for k in keys])
    if args.write:
        if errors:
            sys.exit("本批有失败，产物已保存，未写库；用 --resume 续跑")
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            sys.exit("--write 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        print("written", postgrest_upsert(rows, url, key))
    print(f"comments {len(comments)} · rows {len(rows)} · notes {len(per_note)} · errors {len(errors)}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
