# -*- coding: utf-8 -*-
"""核心流程：一个 subject → 答题调用（+ 证据选句调用）→ 事实 → 账本行。

两类 subject：
  · note 类（TV 格式题库，带 scope）：输入 raw_content + title_extraction，代码切片，题目按 scope 看片段；
    答「是」的题再问一次「依据是哪一句」，选中的原句写进 evidence，答「是」却选「没有」记 evidence_not_found。
  · state 类（Jev 格式题库）：调用方自己给 state（评论、卡片、坐标组合…），一次调用答完。

账本：truth_vault.note_feature_answers（主键 subject_type, subject_id, question_id, question_version, extractor, run_tag）。
prob 统一存「所选答案的概率」（是非题 = max(p, 1-p)，选择题 = 第一名）。歧义不落库，读取时按题库阈值算。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

from . import spans as sp
from .banks import Bank, build_questions, interpret
from .jev_client import JevClient

EVIDENCE_SENT_CHARS = 200


@dataclass
class JudgeResult:
    bank: str
    bank_version: str
    bank_sha256: str
    model: str
    subject_type: str
    subject_id: str
    items: dict                     # qid → {answer, p, ambiguous, evidence?, invalid_reason?, ...}
    skipped: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    latency_ms: list = field(default_factory=list)
    calls: int = 0
    meta: dict = field(default_factory=dict)

    def ambiguous_ids(self) -> list:
        return [q for q, it in self.items.items() if it.get("ambiguous")]


# ── 证据选句 ────────────────────────────────────────────────────────────

def build_evidence_call(bank: Bank, spans_: dict, items: dict) -> Optional[tuple]:
    byid = bank.by_id()
    qs, sent_map = {}, {}
    for qid, it in items.items():
        q = byid.get(qid)
        if not q or not q.needs_evidence(it.get("raw")):
            continue
        sents = sp.split_sentences(sp.scope_text(spans_, q.scope))
        if not sents:
            continue
        sent_map[qid] = sents
        crit = {str(i + 1): s[:EVIDENCE_SENT_CHARS] for i, s in enumerate(sents)}
        crit["没有"] = "没有一句能作为依据"
        what = "「是」" if q.jtype == "noul" else f"「{it.get('answer')}」"
        qs[qid] = {"type": "choice",
                   "instructions": f"上一步对「{q.ask}」答了{what}。下面编号的句子里，哪一句最能作为依据？只选一句；没有就选「没有」。",
                   "criteria": crit}
    if not qs:
        return None
    body = {"state": {"说明": "每道题的选项就是原文里编号的句子，选最能支持该判断的那一句。"}, "questions": qs}
    return body, sent_map


def apply_evidence(items: dict, resp: dict, sent_map: dict) -> None:
    for qid, a in (resp.get("answers") or {}).items():
        if qid not in items or qid not in sent_map:
            continue
        ch = a.get("choice")
        p = float((a.get("probabilities") or {}).get(ch, 0.0))
        if ch == "没有" or ch is None:
            items[qid]["evidence"] = None
            items[qid]["evidence_p"] = round(p, 3)
            items[qid]["invalid_reason"] = "evidence_not_found"
        else:
            try:
                items[qid]["evidence"] = sent_map[qid][int(ch) - 1]
            except (ValueError, IndexError):
                items[qid]["evidence"] = None
                items[qid]["invalid_reason"] = "evidence_not_found"
            items[qid]["evidence_p"] = round(p, 3)


# ── 两类 subject ─────────────────────────────────────────────────────────

def judge_note(client: JevClient, bank: Bank, subject_id: str, raw_content: str, *,
               title_extraction: str = "markers", title_col: Optional[str] = None,
               with_evidence: bool = True, subject_type: str = "note") -> JudgeResult:
    if bank.fmt != "tv":
        raise ValueError("judge_note 只接 TV 格式题库（带 scope）")
    spans_ = sp.build_spans(raw_content, mode=title_extraction, title_col=title_col)
    qids, skipped = sp.askable(bank, spans_)
    body = {"state": sp.state_from_spans(spans_), "model": bank.model, "questions": build_questions(bank, qids)}
    t0 = time.time()
    resp = client.call(body)
    lat = [round((time.time() - t0) * 1000)]
    items = interpret(bank, resp)
    usage = dict(resp.get("usage") or {})
    calls = 1
    if with_evidence:
        ev = build_evidence_call(bank, spans_, items)
        if ev:
            ev_body, sent_map = ev
            ev_body["model"] = bank.model
            t0 = time.time()
            r2 = client.call(ev_body)
            lat.append(round((time.time() - t0) * 1000))
            apply_evidence(items, r2, sent_map)
            u2 = r2.get("usage") or {}
            usage = {k: usage.get(k, 0) + u2.get(k, 0) for k in set(usage) | set(u2)}
            calls += 1
    # 截断规则：body/full 的「否」不能来自没看全的片段（docs/28 §5.3）
    if spans_.get("_truncated"):
        for q in bank.questions:
            it = items.get(q.id)
            if it and q.scope in ("body", "full") and it.get("answer") == "否":
                it["invalid_reason"] = "span_truncated"
    for qid, why in skipped.items():
        items[qid] = {"answer": None, "p": None, "ambiguous": False, "invalid_reason": why}
    return JudgeResult(bank=bank.name, bank_version=bank.version, bank_sha256=bank.sha256, model=bank.model,
                       subject_type=subject_type, subject_id=subject_id, items=items, skipped=skipped, usage=usage,
                       latency_ms=lat, calls=calls,
                       meta={"title_how": spans_.get("_title_how"), "truncated": bool(spans_.get("_truncated"))})


def judge_state(client: JevClient, bank: Bank, subject_id: str, state: dict, *,
                subject_type: str = "comment", qids: Optional[list] = None, fill: Optional[dict] = None) -> JudgeResult:
    """fill：题干 / 定义里 {占位符} 的本篇取值（评论题库的帖子要点、主体称呼）；没有占位符的题库忽略。"""
    body = {"state": state, "model": bank.model, "questions": build_questions(bank, qids, fill=fill)}
    t0 = time.time()
    resp = client.call(body)
    lat = [round((time.time() - t0) * 1000)]
    items = interpret(bank, resp)
    return JudgeResult(bank=bank.name, bank_version=bank.version, bank_sha256=bank.sha256, model=bank.model,
                       subject_type=subject_type, subject_id=subject_id, items=items, usage=dict(resp.get("usage") or {}),
                       latency_ms=lat, calls=1)


# ── 账本行 ───────────────────────────────────────────────────────────────

LEDGER_COLUMNS = ("subject_type", "subject_id", "question_id", "question_version", "bank_version", "bank_sha256",
                  "extractor", "run_tag", "answer", "evidence", "prob", "invalid_reason")


def ledger_rows(result: JudgeResult, bank: Bank, *, run_tag: str = "primary", extractor: Optional[str] = None) -> list:
    extractor = extractor or f"jev:{bank.model.replace('jev-', '')}"
    byid = bank.by_id()
    rows = []
    for qid, it in result.items.items():
        q = byid.get(qid)
        if not q:
            continue
        invalid = it.get("invalid_reason")
        rows.append({
            "subject_type": result.subject_type, "subject_id": result.subject_id, "question_id": qid,
            "question_version": q.version, "bank_version": bank.version, "bank_sha256": bank.sha256,
            "extractor": extractor, "run_tag": run_tag,
            "answer": None if invalid else it.get("answer"),
            "evidence": it.get("evidence") if not invalid else None,
            "prob": it.get("p"), "invalid_reason": invalid,
        })
    return rows


def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def rows_to_sql(rows: list, table: str = "truth_vault.note_feature_answers", batch: int = 200) -> str:
    """生成幂等的 INSERT … ON CONFLICT DO UPDATE，没有 service key 时可经 MCP / psql 执行（同 ingest_gate1_answers --sql-out）。"""
    if not rows:
        return "-- no rows\n"
    cols = ", ".join(LEDGER_COLUMNS)
    key = "(subject_type, subject_id, question_id, question_version, extractor, run_tag)"
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in ("bank_version", "bank_sha256", "answer", "evidence", "prob", "invalid_reason"))
    out = []
    for i in range(0, len(rows), batch):
        vals = ",\n".join("(" + ", ".join(_sql_lit(r.get(c)) for c in LEDGER_COLUMNS) + ")" for r in rows[i:i + batch])
        out.append(f"INSERT INTO {table} ({cols}) VALUES\n{vals}\nON CONFLICT {key} DO UPDATE SET {upd}, extracted_at = now();\n")
    return "\n".join(out)


def postgrest_upsert(rows: list, url: str, service_key: str, table: str = "note_feature_answers",
                     schema: str = "truth_vault", batch: int = 200) -> int:
    """直接写库（需要 service key）。PostgREST 用 Prefer: resolution=merge-duplicates 做 upsert。"""
    import urllib.request
    n = 0
    for i in range(0, len(rows), batch):
        data = json.dumps(rows[i:i + batch], ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{table}", data=data, method="POST", headers={
            "apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json",
            "Content-Profile": schema, "Prefer": "resolution=merge-duplicates,return=minimal"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"PostgREST HTTP {resp.status}")
        n += len(rows[i:i + batch])
    return n


def result_to_dict(r: JudgeResult) -> dict:
    return {"bank": r.bank, "bank_version": r.bank_version, "bank_sha256": r.bank_sha256, "model": r.model,
            "subject_type": r.subject_type, "subject_id": r.subject_id, "items": r.items, "skipped": r.skipped,
            "ambiguous": r.ambiguous_ids(), "usage": r.usage, "latency_ms": r.latency_ms, "calls": r.calls, "meta": r.meta}
