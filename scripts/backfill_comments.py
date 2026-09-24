#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评论回填：给 truth_vault.comments 跑两套题库，出账本 SQL 和每篇的「评论构成」。

  · 运营侧 banks/comment_ops_v0.2.yaml → comment_intent / is_scripted（表里现有六值闭集；state 不带蓝词清单，见题库头注释）
  · 读者侧 banks/comment_reader_v0.3.yaml → 言语行为 / 点名品牌 / 接住帖子 / 细节 / 语域 / 读者用处 / 像不像安排的

  python3 scripts/backfill_comments.py --from-db --limit 500 --sql comments.sql --summary summary.csv [--bank both|ops|reader]
  python3 scripts/backfill_comments.py --input comments.json ...      # [{comment_id, note_id, comment_text, comment_role, title, raw_content}]

账本：subject_type = 'comment'（要先跑 migrations/notes_v1_17_judge_subjects.sql），subject_id = comments.comment_id。
两套题库分别落两批行（question_id 不同，不冲突）。run_tag 默认 primary；先影子跑就传 --run-tag shadow-*。
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

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "banks" / "comment_reader_v0.3.yaml"
OPS = ROOT / "banks" / "comment_ops_v0.2.yaml"


def _pg(url, key, path):
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(limit: int, project: str | None):
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("--from-db 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    q = f"comments?select=comment_id,note_id,content,comment_role,is_pinned&order=note_id,comment_order&limit={limit}"
    if project:
        q += f"&note_id=like.{urllib.parse.quote(project)}*"
    comments = _pg(url, key, q)
    ids = sorted({c["note_id"] for c in comments})
    notes = {}
    for i in range(0, len(ids), 150):
        chunk = ",".join(urllib.parse.quote(x) for x in ids[i:i + 150])
        for n in _pg(url, key, f"notes?select=note_id,title,raw_content&note_id=in.({chunk})"):
            notes[n["note_id"]] = n
    for c in comments:
        n = notes.get(c["note_id"], {})
        c["comment_text"] = c.pop("content", None) or ""
        c["title"] = n.get("title"); c["raw_content"] = n.get("raw_content") or ""
    return comments


def load_input(path: str) -> list:
    """本地文件两种形状：[{comment_id, note_id, comment_text, comment_role, title, raw_content}]，
    或 {"notes": {note_id: raw_content}, "comments": [...]}（正文只写一次）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "comments" in data:
        notes = data.get("notes") or {}
        for c in data["comments"]:
            c.setdefault("raw_content", notes.get(c["note_id"], ""))
            c.setdefault("title", None)
        return data["comments"]
    return data


def head_of(raw: str, n: int = 300) -> str:
    return (raw or "").strip()[:n]


def state_reader(c: dict) -> dict:
    return {"说明": "以下是一篇小红书帖子和它下面的一条评论。只根据帖子和这条评论判断。",
            "帖子标题": c.get("title") or "（没有单独的标题）", "帖子正文": head_of(c.get("raw_content"), 600),
            "评论账号角色": c.get("comment_role") or "未知", "评论原文": c.get("comment_text") or ""}


def state_ops(c: dict) -> dict:
    st = {"说明": "以下是一篇小红书帖子和运营在它下面写的一条评论。判断这条评论在帖子下面想起什么作用。",
          "帖子标题": c.get("title") or "（没有单独的标题）", "帖子正文": head_of(c.get("raw_content"), 300),
          "评论角色": c.get("comment_role") or "未知", "评论原文": c.get("comment_text") or ""}
    # 不给蓝词清单（co-v0.2）：那是项目 / 品牌层信息（D-079、Mode A）；评论里有没有目标蓝词由 TV 的 contains_blue_keyword 代码列管
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-db", action="store_true"); ap.add_argument("--input"); ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--project"); ap.add_argument("--bank", choices=["both", "ops", "reader"], default="both")
    ap.add_argument("--run-tag", default="primary"); ap.add_argument("--sql"); ap.add_argument("--summary"); ap.add_argument("--raw")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--workers", type=int, default=4); ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    comments = fetch(args.limit, args.project) if args.from_db else load_input(args.input)
    banks = []
    if args.bank in ("both", "ops"):
        banks.append((load_bank(OPS, name="comment_ops_v0.2"), state_ops))
    if args.bank in ("both", "reader"):
        banks.append((load_bank(READER, name="comment_reader_v0.3"), state_reader))
    try:
        client = JevClient(mock=args.mock)
    except JevError as exc:
        sys.exit(str(exc))

    rows, raw, per_note = [], [], defaultdict(Counter)

    def one(c):
        out = []
        for bank, mk in banks:
            r = judge_state(client, bank, c["comment_id"], mk(c), subject_type="comment")
            out.append((bank, r))
        return c, out

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for c, outs in ex.map(one, comments):
            for bank, r in outs:
                rows.extend(ledger_rows(r, bank, run_tag=args.run_tag))
                raw.append({"comment_id": c["comment_id"], "note_id": c["note_id"], "bank": bank.name, "items": r.items})
                sa = r.items.get("speech_act", {}).get("answer")
                if sa:
                    per_note[c["note_id"]][sa] += 1
                ci = r.items.get("comment_intent", {}).get("answer")
                if ci:
                    per_note[c["note_id"]]["运营:" + ci] += 1
    if args.sql:
        Path(args.sql).write_text(rows_to_sql(rows), encoding="utf-8")
    if args.raw:
        Path(args.raw).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.summary:
        keys = sorted({k for c in per_note.values() for k in c})
        with open(args.summary, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["note_id", "comments"] + keys)
            for nid, cnt in per_note.items():
                w.writerow([nid, sum(v for k, v in cnt.items() if not k.startswith("运营:"))] + [cnt.get(k, 0) for k in keys])
    if args.write:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            sys.exit("--write 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        print("written", postgrest_upsert(rows, url, key))
    print(f"comments {len(comments)} · rows {len(rows)} · notes {len(per_note)}")


if __name__ == "__main__":
    main()
