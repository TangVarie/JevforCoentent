# -*- coding: utf-8 -*-
"""HTTP 服务：POST /judge · GET /banks · GET /health。

  POST /judge
    {
      "bank": "feature_questions_v0_1",          # banks/ 下的文件名（不带 .yaml）
      "run_tag": "shadow-2026-09",               # 落库时用；不落库可省
      "write": false,                            # true = 直接写 note_feature_answers（需要 SUPABASE_URL + SUPABASE_SERVICE_KEY）
      "with_evidence": true,                     # note 类：是否做「依据是哪一句」
      "subjects": [
        {"subject_type": "note", "subject_id": "NUC_phase1_recv…", "raw_content": "…", "title_extraction": "markers"},
        {"subject_type": "comment", "subject_id": "…", "state": {"帖子标题": "…", "评论原文": "…"},
         "fill": {"subject": "这个产品", "post_points": "…"}}   # 题干里的占位符（评论题库 v0.4 / v0.3 按篇填）
      ]
    }
  返回每个 subject 的 items（答案、概率、歧义、证据）、歧义题列表、用量与延迟；write=true 时另返回写入行数。

服务端只认 X-Judge-Key（JUDGE_API_KEY 环境变量），Jev 密钥不出服务端。失败不重试调用方，调用方自己 fail-open。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .banks import Bank, check_bank, discover, load_bank, unfilled
from .core import judge_note, judge_state, ledger_rows, postgrest_upsert, result_to_dict
from .jev_client import JevClient, JevError

BANKS_DIR = Path(os.environ.get("JUDGE_BANKS_DIR", Path(__file__).resolve().parent.parent / "banks"))
_BANK_CACHE: dict = {}


def get_bank(name: str) -> Bank:
    if name in _BANK_CACHE:
        return _BANK_CACHE[name]
    found = discover(BANKS_DIR)
    if name not in found:
        raise HTTPException(404, f"没有这个题库：{name}（有：{sorted(found)}）")
    bank = load_bank(found[name], name=name)
    problems = check_bank(bank)
    if problems:
        raise HTTPException(500, f"题库 {name} 有问题：{problems}")
    _BANK_CACHE[name] = bank
    return bank


def require_key(x_judge_key: Optional[str] = Header(default=None)):
    want = os.environ.get("JUDGE_API_KEY", "")
    if want and x_judge_key != want:
        raise HTTPException(401, "X-Judge-Key 不对")


class Subject(BaseModel):
    subject_type: str = "note"
    subject_id: str
    raw_content: Optional[str] = None
    title_extraction: str = "markers"
    title_col: Optional[str] = None
    state: Optional[dict] = None
    qids: Optional[list] = None
    fill: Optional[dict] = None


class JudgeRequest(BaseModel):
    bank: str
    subjects: list[Subject] = Field(min_length=1, max_length=200)
    run_tag: str = "primary"
    write: bool = False
    with_evidence: bool = True
    extractor: Optional[str] = None


app = FastAPI(title="judge", version=__version__)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "banks": sorted(discover(BANKS_DIR)),
            "jev_key": bool(os.environ.get("TYPESAFE_API_KEY")), "write_enabled": bool(os.environ.get("SUPABASE_SERVICE_KEY"))}


@app.get("/banks", dependencies=[Depends(require_key)])
def banks():
    out = []
    for name, path in discover(BANKS_DIR).items():
        b = load_bank(path, name=name)
        out.append({"name": name, "version": b.version, "model": b.model, "format": b.fmt,
                    "questions": b.ids(), "sha256": b.sha256, "problems": check_bank(b)})
    return out


@app.post("/judge", dependencies=[Depends(require_key)])
def judge(req: JudgeRequest):
    bank = get_bank(req.bank)
    try:
        client = JevClient(mock=os.environ.get("JUDGE_MOCK") == "1")
    except JevError as exc:
        raise HTTPException(503, str(exc))
    results, rows = [], []
    for s in req.subjects:
        try:
            if bank.fmt == "tv":
                if s.raw_content is None:
                    raise HTTPException(422, f"{s.subject_id}: TV 格式题库需要 raw_content")
                r = judge_note(client, bank, s.subject_id, s.raw_content, title_extraction=s.title_extraction,
                               title_col=s.title_col, with_evidence=req.with_evidence, subject_type=s.subject_type)
            else:
                if s.state is None:
                    raise HTTPException(422, f"{s.subject_id}: Jev 格式题库需要 state")
                left = unfilled(bank, s.fill)
                if left:
                    raise HTTPException(422, f"{s.subject_id}: 题库 {bank.name} 的占位符没填全：{left}（用 fill 给）")
                r = judge_state(client, bank, s.subject_id, s.state, subject_type=s.subject_type, qids=s.qids, fill=s.fill)
        except JevError as exc:
            raise HTTPException(502, f"Jev 调用失败：{exc}")
        results.append(result_to_dict(r))
        rows.extend(ledger_rows(r, bank, run_tag=req.run_tag, extractor=req.extractor))
    written = None
    if req.write:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
        if not (url and key):
            raise HTTPException(503, "没配 SUPABASE_URL / SUPABASE_SERVICE_KEY，不能写库")
        written = postgrest_upsert(rows, url, key)
    return {"bank": bank.name, "bank_version": bank.version, "model": bank.model, "results": results,
            "rows": len(rows), "written": written}
