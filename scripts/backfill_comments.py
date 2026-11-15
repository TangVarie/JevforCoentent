#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评论回填：给 truth_vault.comments 跑两套题库，出账本 SQL 和每篇的「评论构成」。

  · 运营侧 banks/comment_ops_v0.2.yaml → comment_intent / is_scripted（表里现有六值闭集；state 不带蓝词清单，见题库头注释）
  · 读者侧 banks/comment_reader_v0.6.yaml（C4，D-112 门槛轮通过、owner 10-16 验收）→ 言语行为 / 点名品牌 / 接住帖子 / 细节
    四道门槛题 + 语域 / 读者用处 / 像不像安排的三道参考题；账本照常每题一行，参考题只提示是写手工具的口径，回填不区分

  python3 scripts/backfill_comments.py --from-db --limit 500 --sql comments.sql --summary summary.csv [--bank both|ops|reader]
  python3 scripts/backfill_comments.py --input comments.json ...      # [{comment_id, note_id, comment_text, comment_role, title, raw_content}]
  python3 scripts/backfill_comments.py --resume-ids ids.json --resume rows.json ...   # 续跑：按 id 快照回生产重取同一批

续跑快照（--input-snapshot）只存 comment_id / note_id / 角色 / md5（评论、帖子正文、模型实际看到的整份输入），
不存评论原文和帖子正文：快照会作为 Actions 产物上传，原文不进 GitHub（D-112「不做」）。续跑按 id 回生产重取，
任何一项对不上就拒绝（含标题、品类变了），不判变了的输入。

账本：subject_type = 'comment'（要先跑 migrations/notes_v1_17_judge_subjects.sql），subject_id = comments.comment_id。
两套题库分别落两批行（question_id 不同，不冲突）。默认 shadow；primary 写库需 --acceptance 指向人工验收记录。
答案只落账本，不回写 comments 表的 comment_intent / comment_type / is_scripted 三列（docs/31 §3 ② 的决定：
业务表归 TV 的同步脚本管，judge 不改 TV 业务表；要按列看就从账本 join）。
只读 comments / notes；写库用 --write（需要 SUPABASE_SERVICE_ROLE_KEY），否则只出 SQL。

两种来源分开算（TV notes_v1_27 给 comments 加了 source / is_truncated / observed_at 三列之后）：
  · 运营写的（随贴评论那一格，source 为空或运营侧取值）：跟以前完全一样，按 --bank 跑。
  · 巡查来的（链接巡查抄回来的评论区首页，source = linkcheck_digest / linkcheck_records）：读者的评论只跑读者侧题库，
    不跑运营侧题库。其中两种不算读者评论、一题都不跑、各自单独报数：
      - 命中预埋关键词的（TV 写 is_scripted = true，多半是我们自己埋的）；
      - 贴主自己的回复（TV 写 comment_role = 贴主，是账号自己说的话）。
    这跟 TV v1.27 给「读者那一侧」下的定义一致：source 是 linkcheck_*、is_scripted 不是 true、comment_role 不是贴主。
  · source 是不认识的取值：一题都不跑，只报数（不猜它算哪一边）。
  汇总表里两种来源各占各的列（运营写的还是原来那几列，读者评论的列带「读者:」前缀），没有哪个数是两边加在一起的。
  续跑时，快照里巡查来的行如果生产里已经没有了（首页评论区变了，TV 夜里的同步会把旧的摘要行删掉，这是正常的），
  跳过这几条、报个数、其余照常续跑；运营写的行不见了仍然拒绝续跑。
  --source operator|digest 可以只跑一边，默认 all。
  这三列还不存在时（v1.27 还没跑）自动退回老的取数，提示一次，其余同以前。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import json
import os
import sys
import urllib.error
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
READER = ROOT / "banks" / "comment_reader_v0.6.yaml"
READER_NAME = "comment_reader_v0.6"
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


COMMENT_COLS = "comment_id,note_id,content,comment_role,is_pinned"
# TV notes_v1_27 加的三列 + is_scripted（v1.2 就有的老列；v1.27 起 TV 给命中预埋关键词的巡查行写 true）。
# 三列不存在时不带这一段，见 _comment_pages。
SOURCE_COLS = "source,is_truncated,observed_at,is_scripted"
NEW_COLUMNS = ("source", "is_truncated", "observed_at")

# 一条评论算哪一边。取值照 TV 实际写的（scripts/sync_comments_from_raw_extra.py 的 SOURCE_DIGEST / SOURCE_RECORDS、
# is_scripted、comment_role；schemas/notes_v1_27 的列注释），改那边要同步改这里：
#   source 为空                    = 运营写的
#   source 以 linkcheck_ 开头      = 巡查来的；其中 is_scripted 是 true = 命中预埋关键词，comment_role 是贴主 = 贴主自己的回复，
#                                    其余才是读者评论（代码里沿用 digest 这个名字，含 linkcheck_records 明细行）
#   别的取值                       = 认不出，不猜
OPERATOR, DIGEST, SEEDED, AUTHOR, UNKNOWN = "operator", "digest", "seeded", "author", "unknown"
LINKCHECK_PREFIX = "linkcheck_"
AUTHOR_ROLE = "贴主"
READER_PREFIX = "读者:"

_columns = {"present": None, "warned": False}     # None = 还没问过；False = 这一轮确认三列不存在


def _truthy(v) -> bool:
    return v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0) \
        or (isinstance(v, str) and v.strip().lower() in ("1", "true", "t", "yes", "y", "是"))


def source_kind(c: dict) -> str:
    """一条评论算哪一边：operator（运营写的）/ digest（巡查来的读者评论）/ seeded（巡查来的、命中预埋关键词）/
    author（巡查来的、贴主自己的回复）/ unknown。
    没有 source 键或值为空 = 运营写的（v1.27 之前的行、以及本地输入文件），这时不看 is_scripted 和角色。
    认不出的取值不猜，归 unknown。"""
    src = str(c.get("source") or "").strip().lower()
    if not src:
        return OPERATOR
    if not src.startswith(LINKCHECK_PREFIX):
        return UNKNOWN
    if c.get("is_scripted") is True:
        return SEEDED
    if str(c.get("comment_role") or "").strip() == AUTHOR_ROLE:
        return AUTHOR
    return DIGEST


def _missing_new_column(exc: Exception) -> bool:
    """只认「v1.27 那三列还没有」这一种错：PostgREST 400 + Postgres 42703（undefined_column）+ 报的正是那三列之一。
    别的 400、权限、网络错都照常抛。"""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 400:
        return False
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(body, dict) or body.get("code") != "42703":
        return False
    msg = str(body.get("message") or "")
    return any(f"comments.{col} " in msg or f"column {col} " in msg or f'"{col}"' in msg for col in NEW_COLUMNS)


def _comment_pages(url, key, tail: str, limit: int) -> list:
    """取评论行：先带上 v1.27 的三列；库里还没有这三列就提示一次，这一轮都按老的列取（行为同以前）。"""
    if _columns["present"] is not False:
        try:
            rows = _pg_pages(url, key, f"comments?select={COMMENT_COLS},{SOURCE_COLS}{tail}", limit)
            _columns["present"] = True
            return rows
        except urllib.error.HTTPError as exc:
            if not _missing_new_column(exc):
                raise
            _columns["present"] = False
            if not _columns["warned"]:
                _columns["warned"] = True
                print("提示：truth_vault.comments 还没有 source / is_truncated / observed_at（TV notes_v1_27 没跑），"
                      "这一轮所有评论按运营写的处理，同以前。", file=sys.stderr)
    return _pg_pages(url, key, f"comments?select={COMMENT_COLS}{tail}", limit)


def _db():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("--from-db / --resume-ids 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    return url, key


def fetch(limit: int, project: str | None):
    url, key = _db()
    q = "&order=note_id,comment_order,comment_id"
    if project:
        q += f"&note_id=like.{urllib.parse.quote(project)}*"
    return _with_notes(url, key, _comment_pages(url, key, q, limit))


def _md5(text) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _input_md5(c: dict) -> str:
    """模型实际看到的全部输入（state + 占位符取值）：标题、品类（决定 agency / product 占位符）变了也算变。"""
    return _md5(json.dumps({"state": state_reader(c), "fill": fill_for(post_for(c))}, ensure_ascii=False, sort_keys=True))


def _digests(c: dict) -> list:
    return [_md5(c.get("comment_text")), _md5(c.get("raw_content")), _input_md5(c)]


def snapshot_of(comments: list) -> list:
    """续跑快照：只有 id 和 md5，没有原文（产物会上传到 Actions）。"""
    out = []
    for c in comments:
        s = {"comment_id": c["comment_id"], "note_id": c["note_id"], "role": c.get("comment_role"), "md5": _digests(c)}
        if source_kind(c) != OPERATOR:          # 运营写的不加这个键：快照形状同以前，老快照也照样能续跑
            s["source_kind"] = source_kind(c)
        out.append(s)
    return out


def fetch_ids(snapshot: list) -> list:
    """按快照的 id 回生产重取同一批评论（保持快照顺序）；运营写的行缺了、换帖或文本变了都拒绝续跑。
    巡查来的行不一样：首页评论区一变，TV 夜里的同步就把旧的摘要行删掉（id 是按内容算的，内容变了就是另一行），
    所以这种行不见了是正常的 —— 跳过它、报个数，其余照常续跑（它上一轮的答案不会再进账本）。
    巡查来的行在「读者评论 / 命中预埋关键词 / 贴主回复」之间换了也不拒绝（比如运营改了评论关键词那一格）：
    正文没变，按现在的归类跑；现在不该有题的，旧答案在 main 里丢掉。运营写的和巡查来的之间互换仍然拒绝。"""
    url, key = _db()
    ids = [s["comment_id"] for s in snapshot]
    got = {}
    for i in range(0, len(ids), 150):
        part = ids[i:i + 150]
        chunk = ",".join(urllib.parse.quote(x) for x in part)
        for c in _comment_pages(url, key, f"&comment_id=in.({chunk})&order=comment_id", len(part)):
            got[c["comment_id"]] = c
    gone = [s for s in snapshot if s["comment_id"] not in got]
    missing = [s for s in gone if s.get("source_kind", OPERATOR) == OPERATOR]
    if missing:
        raise ValueError(f"{len(missing)} 条快照里的评论生产里已经找不到，拒绝续跑")
    if gone:
        print(f"提示：快照里有 {len(gone)} 条巡查来的评论生产里已经没有了（首页评论区变了，旧的摘要行被同步删掉，属正常），"
              "这几条跳过，其余照常续跑。", file=sys.stderr)
        snapshot = [s for s in snapshot if s["comment_id"] in got]
    comments = _with_notes(url, key, [got[s["comment_id"]] for s in snapshot])

    def moved(s, c):        # 运营写的 ↔ 巡查来的 = 要跑的题库整套变了
        return (s.get("source_kind", OPERATOR) == OPERATOR) != (source_kind(c) == OPERATOR)
    drift = [s["comment_id"] for s, c in zip(snapshot, comments)
             if (s["note_id"], s.get("role")) != (c["note_id"], c.get("comment_role"))
             or moved(s, c) or s["md5"] != _digests(c)]
    if drift:
        raise ValueError(f"{len(drift)} 条评论或所属帖子（正文 / 标题 / 品类）跟快照对不上，拒绝续跑（续跑只判同一批输入）")
    return comments


def _with_notes(url: str, key: str, comments: list) -> list:
    ids = sorted({c["note_id"] for c in comments})
    notes = {}
    for i in range(0, len(ids), 150):
        chunk = ",".join(urllib.parse.quote(x) for x in ids[i:i + 150])
        for n in _pg_pages(url, key, f"notes?select=note_id,title,raw_content,project_id,projects(category)&note_id=in.({chunk})&order=note_id",
                           len(ids[i:i + 150])):
            notes[n["note_id"]] = n
    missing = set(ids) - set(notes)
    if missing:
        raise ValueError(f"{len(missing)} 篇笔记上下文未取全，拒绝用空正文判评论")
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


def banks_for(c: dict, banks: list) -> list:
    """这条评论跑哪几套题库：运营写的 = 选中的全部（同以前）；巡查来的读者评论 = 只跑读者侧；
    命中预埋关键词的、贴主自己回复的、来源认不出的 = 一套都不跑。"""
    kind = source_kind(c)
    if kind == OPERATOR:
        return list(banks)
    if kind == DIGEST:
        return [(bank, mk) for bank, mk in banks if bank.name == READER_NAME]
    return []


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
    ap.add_argument("--source", choices=["all", "operator", "digest"], default="all",
                    help="只跑运营写的 / 只跑巡查来的读者评论；默认两边都跑，各算各的")
    ap.add_argument("--run-tag", default="shadow-comments"); ap.add_argument("--sql"); ap.add_argument("--summary"); ap.add_argument("--raw")
    ap.add_argument("--rows-json"); ap.add_argument("--resume", help="完整有效的同题库/模型/run_tag 账本行才跳过")
    ap.add_argument("--input-snapshot", help="保存本批 id + md5 快照（不含原文），续跑用 --resume-ids 指向它")
    ap.add_argument("--resume-ids", help="续跑：按上一轮的 id 快照回生产重取同一批评论，文本变了就拒绝")
    ap.add_argument("--acceptance", help="primary 写库前的 owner 人工验收记录 JSON")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--workers", type=int, default=4); ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    if sum(map(bool, (args.from_db, args.input, args.resume_ids))) != 1:
        ap.error("必须且只能指定 --from-db、--input、--resume-ids 之一")
    if not 1 <= args.limit <= 30000 or not 1 <= args.workers <= 8:
        ap.error("limit 需为 1..30000，workers 需为 1..8")
    if args.mock and args.write:
        ap.error("mock 答案不允许写库")
    banks = []
    if args.bank in ("both", "ops"):
        banks.append((load_bank(OPS, name="comment_ops_v0.2"), state_ops))
    if args.bank in ("both", "reader"):
        banks.append((load_bank(READER, name=READER_NAME), state_reader))
    if args.write and not args.run_tag.startswith("shadow-"):
        try:
            validate_acceptance(args.acceptance, banks)
        except (ValueError, OSError) as exc:
            ap.error(str(exc))
    if args.resume_ids:
        try:
            comments = fetch_ids(json.loads(Path(args.resume_ids).read_text(encoding="utf-8")))[:args.limit]
        except (ValueError, OSError) as exc:
            sys.exit(str(exc))
    else:
        comments = fetch(args.limit, args.project) if args.from_db else load_input(args.input)[:args.limit]
    if len({c["comment_id"] for c in comments}) != len(comments):
        ap.error("输入包含重复 comment_id")
    kinds = {c["comment_id"]: source_kind(c) for c in comments}
    if args.source != "all":
        comments = [c for c in comments if kinds[c["comment_id"]] == args.source]
    by_kind = Counter(kinds[c["comment_id"]] for c in comments)
    if args.input_snapshot:
        Path(args.input_snapshot).write_text(json.dumps(snapshot_of(comments), ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        client = JevClient(mock=args.mock)
    except JevError as exc:
        sys.exit(str(exc))

    previous = json.loads(Path(args.resume).read_text(encoding="utf-8")) if args.resume else []
    note_ids = {c["comment_id"]: c["note_id"] for c in comments}
    bank_by_sha = {bank.sha256: bank for bank, _ in banks}
    allowed = {(c["comment_id"], bank.sha256) for c in comments for bank, _ in banks_for(c, banks)}
    resumed = defaultdict(list)
    rows_by_key = {}
    for row in previous:
        bank = bank_by_sha.get(row.get("bank_sha256"))
        if (bank is None or row.get("subject_type") != "comment" or row.get("subject_id") not in note_ids
                or row.get("run_tag") != args.run_tag
                or row.get("question_id") not in bank.by_id()
                or row.get("extractor") != "jev:" + bank.model.replace("jev-", "")):
            continue
        if (row["subject_id"], bank.sha256) not in allowed:
            continue        # 现在不该有题的行（命中预埋关键词 / 贴主回复 / 来源认不出 / 读者评论的运营侧题）：旧答案不再带进账本和统计
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
        for bank, mk in banks_for(c, banks):
            if not args.mock and complete(resumed[c["comment_id"], bank.sha256], c["comment_id"], bank, args.run_tag):
                continue
            try:
                r = judge_state(client, bank, c["comment_id"], mk(c), subject_type="comment", fill=fill_for(post_for(c)))
            except Exception as exc:  # Preserve successful banks even for malformed nested model JSON.
                out.append((bank, f"{type(exc).__name__}: {exc}"))
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
                entry = {"comment_id": c["comment_id"], "note_id": c["note_id"], "bank": bank.name, "items": r.items}
                if kinds[c["comment_id"]] != OPERATOR:
                    entry.update(source_kind=kinds[c["comment_id"]], is_truncated=c.get("is_truncated"),
                                 observed_at=c.get("observed_at"))
                raw.append(entry)
                if any(it.get("invalid_reason") for it in r.items.values()):
                    errors.append({"comment_id": c["comment_id"], "bank": bank.name, "error": "缺答/选项外，续跑重试"})
            if index % 25 == 0:
                checkpoint()
    checkpoint()
    rows = list(rows_by_key.values())
    for row in rows:
        if row.get("subject_id") not in note_ids or row.get("run_tag") != args.run_tag or not row.get("answer"):
            continue
        kind = kinds[row["subject_id"]]
        if kind == DIGEST:                      # 读者评论只有读者侧的题；单独一组列，不跟运营写的加在一起
            if row["question_id"] == "speech_act" and bank_by_sha.get(row.get("bank_sha256")) is not None \
                    and bank_by_sha[row["bank_sha256"]].name == READER_NAME:
                per_note[note_ids[row["subject_id"]]][READER_PREFIX + str(row["answer"])] += 1
            continue
        if kind != OPERATOR:                    # 续跑文件里以前判过、现在归到「不算」的行：不进任何统计
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
        every = {k for c in per_note.values() for k in c}
        keys = sorted(k for k in every if not k.startswith(READER_PREFIX))
        rkeys = sorted(k for k in every if k.startswith(READER_PREFIX))
        seeded = Counter(c["note_id"] for c in comments if kinds[c["comment_id"]] == SEEDED)
        author = Counter(c["note_id"] for c in comments if kinds[c["comment_id"]] == AUTHOR)
        cut = Counter(c["note_id"] for c in comments if kinds[c["comment_id"]] == DIGEST and _truthy(c.get("is_truncated")))
        split = bool(by_kind[DIGEST] or by_kind[SEEDED] or by_kind[AUTHOR])   # 没有巡查来的行（含三列还不存在）：表头和以前一模一样
        with open(args.summary, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["note_id", "comments"] + keys
                       + (["reader_comments", "reader_truncated", "seeded_hits_skipped", "author_replies_skipped"] + rkeys
                          if split else []))
            for nid in list(per_note) + list(dict.fromkeys(n for n in list(seeded) + list(author) + list(cut)
                                                           if n not in per_note)):
                cnt = per_note.get(nid, Counter())
                line = [nid, sum(v for k, v in cnt.items() if not k.startswith(("运营:", READER_PREFIX)))] \
                    + [cnt.get(k, 0) for k in keys]
                if split:
                    line += [sum(v for k, v in cnt.items() if k.startswith(READER_PREFIX)), cut.get(nid, 0),
                             seeded.get(nid, 0), author.get(nid, 0)] + [cnt.get(k, 0) for k in rkeys]
                w.writerow(line)
    if args.write:
        if errors:
            sys.exit("本批有失败，产物已保存，未写库；用 --resume 续跑")
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            sys.exit("--write 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        print("written", postgrest_upsert(rows, url, key))
    print(f"comments {len(comments)} · rows {len(rows)} · notes {len(per_note)} · errors {len(errors)}")
    if set(by_kind) - {OPERATOR}:
        print(f"来源：运营写的 {by_kind[OPERATOR]} · 巡查来的读者评论 {by_kind[DIGEST]}（只跑读者侧）"
              f" · 命中预埋关键词不算读者评论 {by_kind[SEEDED]} · 贴主自己的回复不算读者评论 {by_kind[AUTHOR]}"
              f" · 来源认不出没跑 {by_kind[UNKNOWN]}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
