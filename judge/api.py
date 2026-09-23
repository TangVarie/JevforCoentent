# -*- coding: utf-8 -*-
"""HTTP 服务：POST /judge · POST /judge_draft · GET /banks · GET /health。

  POST /judge   （一个题库、一批 subject；特征层 / 评论 / 外部语料都走这里）
    {
      "bank": "feature_questions_v0_1",          # banks/ 下的文件名（不带 .yaml）
      "run_tag": "shadow-2026-09",               # 落库时用；不落库可省
      "write": false,                            # true = 直接写 note_feature_answers（需要 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY）
      "return_rows": true,                       # 把账本行原样带回（调用方自己落库时用）
      "with_evidence": true,                     # note 类：是否做「依据是哪一句」
      "subjects": [
        {"subject_type": "note", "subject_id": "NUC_phase1_recv…", "raw_content": "…", "title_extraction": "markers"},
        {"subject_type": "comment", "subject_id": "…", "state": {"帖子标题": "…", "评论原文": "…"},
         "fill": {"subject": "这个产品", "post_points": "…"}}   # 题干里的占位符（评论题库 v0.4 / v0.3 按篇填）
      ]
    }
    subjects 并行判（JUDGE_WORKERS，默认 4 路），单个 subject 的 Jev 失败只让它自己带 error，不拖累整批；全部失败才 502。
    返回每个 subject 的 items（答案、概率、歧义、证据）、歧义题列表、用量与延迟；write=true 时另返回写入行数。

  POST /judge_draft   （一篇稿子、多层题库、带修改单；写作台 commit_drafts 挂的是它）
    {
      "title": "…", "body": "…", "subject_id": "<versions.id>", "subject_type": "aw_version",
      "banks": ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.1"],
      "brief": {"intents": [...], "hard_rules": [{"id": "no_price", "ask": "…", "want": false}], "angle": {...}},   # 可选：按 brief 现编项目题库
      "project_bank": {…Jev 格式题库内容…},       # 可选：或直接内联一份项目题库
      "hard_rules": {"platform_health_v0.1:efficacy_claim": "否"},   # 可选：不给用默认（平台三题 + fq efficacy_promise 为「否」）+ brief 的 want
      "target": {"opening_type": "具体事件"}, "validated": ["opening_type"],   # 可选：目标画像 / 过了闸二的 fq 题
      "judge_paras": "on_fail", "run_tag": "primary", "write": false, "return_rows": true
    }
    返回 passed / profile / hard_fails / ambiguous / para_stats / plan（修改单）/ detail / 账本行。

鉴权：X-Judge-Key 必须等于 JUDGE_API_KEY；没配 JUDGE_API_KEY 时默认一律 503 拒绝（fail-closed），本地开发显式设 JUDGE_ALLOW_ANONYMOUS=1。
/health 不鉴权（Railway 探活），只回布尔与题库名，不回密钥。Jev 密钥不出服务端；调用方自己 fail-open。
"""
from __future__ import annotations

import concurrent.futures as cf
import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from . import spans as sp
from .banks import Bank, check_bank, discover, load_bank, load_bank_data, unfilled
from .core import judge_note, judge_state, ledger_rows, postgrest_upsert, result_to_dict
from .jev_client import JevClient, JevError
from .loop import DEFAULT_HARD_RULES, compile_project_bank, judge_draft as _judge_draft, project_hard_rules, repair_plan

BANKS_DIR = Path(os.environ.get("JUDGE_BANKS_DIR", Path(__file__).resolve().parent.parent / "banks"))
TITLE_MODES = set(getattr(sp._tv, "TITLE_EXTRACTION_MODES", ("column", "markers", "none")))
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


# ── 鉴权（fail-closed，同 TV service_auth）────────────────────────────────

def auth_mode() -> str:
    if os.environ.get("JUDGE_API_KEY", ""):
        return "key"
    if os.environ.get("JUDGE_ALLOW_ANONYMOUS") == "1":
        return "anonymous"
    return "unconfigured"


def require_key(x_judge_key: Optional[str] = Header(default=None)):
    mode = auth_mode()
    if mode == "unconfigured":
        raise HTTPException(503, "JUDGE_API_KEY 未配置：服务默认拒绝所有请求；本地开发请显式设 JUDGE_ALLOW_ANONYMOUS=1")
    if mode == "key" and not hmac.compare_digest((x_judge_key or "").encode("utf-8"), os.environ["JUDGE_API_KEY"].encode("utf-8")):
        raise HTTPException(401, "X-Judge-Key 不对")


def workers() -> int:
    try:
        return max(1, int(os.environ.get("JUDGE_WORKERS", "4")))
    except ValueError:
        return 4


def _client() -> JevClient:
    try:
        return JevClient(mock=os.environ.get("JUDGE_MOCK") == "1")
    except JevError as exc:
        raise HTTPException(503, str(exc))


def _write_rows(rows: list) -> int:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        raise HTTPException(503, "没配 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，不能写库")
    return postgrest_upsert(rows, url, key)


# ── /judge ───────────────────────────────────────────────────────────────

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
    return_rows: bool = True


def _validate_subjects(bank: Bank, subjects: list) -> None:
    """输入形状先整批校验完再调 Jev，422 不会在跑了一半时才出现。"""
    for s in subjects:
        if bank.fmt == "tv":
            if s.raw_content is None:
                raise HTTPException(422, f"{s.subject_id}: TV 格式题库需要 raw_content")
            if s.title_extraction not in TITLE_MODES:
                raise HTTPException(422, f"{s.subject_id}: title_extraction 只能是 {sorted(TITLE_MODES)}")
        else:
            if s.state is None:
                raise HTTPException(422, f"{s.subject_id}: Jev 格式题库需要 state")
            left = unfilled(bank, s.fill)
            if left:
                raise HTTPException(422, f"{s.subject_id}: 题库 {bank.name} 的占位符没填全：{left}（用 fill 给）")


def _judge_one(client: JevClient, bank: Bank, s: Subject, with_evidence: bool):
    try:
        if bank.fmt == "tv":
            return judge_note(client, bank, s.subject_id, s.raw_content, title_extraction=s.title_extraction,
                              title_col=s.title_col, with_evidence=with_evidence, subject_type=s.subject_type)
        return judge_state(client, bank, s.subject_id, s.state, subject_type=s.subject_type, qids=s.qids, fill=s.fill)
    except JevError as exc:
        return {"subject_type": s.subject_type, "subject_id": s.subject_id, "error": f"Jev 调用失败：{exc}"}


def _parallel(fn, items: list) -> list:
    n = min(workers(), len(items))
    if n <= 1:
        return [fn(x) for x in items]
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(fn, items))


app = FastAPI(title="judge", version=__version__)


@app.get("/health")
def health():
    mode = auth_mode()
    return {"ok": True, "version": __version__, "banks": sorted(discover(BANKS_DIR)),
            "auth": {"mode": mode, "required": mode == "key"}, "workers": workers(),
            "jev_key": bool(os.environ.get("TYPESAFE_API_KEY")), "mock": os.environ.get("JUDGE_MOCK") == "1",
            "write_enabled": bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))}


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
    _validate_subjects(bank, req.subjects)
    client = _client()
    outs = _parallel(lambda s: _judge_one(client, bank, s, req.with_evidence), req.subjects)
    results, rows, errors = [], [], 0
    for r in outs:
        if isinstance(r, dict):
            results.append(r); errors += 1
            continue
        results.append(result_to_dict(r))
        rows.extend(ledger_rows(r, bank, run_tag=req.run_tag, extractor=req.extractor))
    if errors == len(outs):
        raise HTTPException(502, results[0]["error"])
    written = _write_rows(rows) if req.write else None
    return {"bank": bank.name, "bank_version": bank.version, "model": bank.model, "results": results,
            "rows": len(rows), "errors": errors, "ledger_rows": rows if req.return_rows else None, "written": written}


# ── /judge_draft ─────────────────────────────────────────────────────────

class DraftRequest(BaseModel):
    title: str = ""
    body: str
    subject_id: str = "draft"
    subject_type: str = "aw_version"
    banks: list[str] = Field(default_factory=lambda: ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.1"])
    brief: Optional[dict] = None
    project_bank: Optional[dict] = None
    project_bank_name: str = "project"
    hard_rules: Optional[dict] = None       # {"bank:qid": 期望答案}
    target: Optional[dict] = None
    validated: Optional[list] = None
    judge_paras: str = "on_fail"
    run_tag: str = "primary"
    write: bool = False
    return_rows: bool = True


def _split_banks(names: list) -> tuple:
    """按名字分层：fq = 第一个 TV 格式；platform / human 按前缀；其余 Jev 格式的当项目题库（最多一份）。"""
    fq = platform = human = project = None
    loaded = {}
    for n in names:
        b = get_bank(n); loaded[n] = b
        if b.fmt == "tv" and fq is None:
            fq = b
        elif n.startswith("platform"):
            platform = b
        elif n.startswith("human_feel"):
            human = b
        elif b.fmt == "jev" and not n.startswith("comment") and not n.startswith("external") and project is None:
            project = b
    return loaded, fq, platform, human, project


@app.post("/judge_draft", dependencies=[Depends(require_key)])
def judge_draft(req: DraftRequest):
    if req.judge_paras not in ("always", "on_fail", "never"):
        raise HTTPException(422, "judge_paras 只能是 always / on_fail / never")
    loaded, fq, platform, human, project = _split_banks(req.banks)
    hard = dict(DEFAULT_HARD_RULES)
    if req.project_bank is not None:
        project = load_bank_data(req.project_bank, req.project_bank_name)
        problems = check_bank(project)
        if problems:
            raise HTTPException(422, f"内联项目题库有问题：{problems}")
    elif req.brief is not None:
        project = compile_project_bank(req.brief, name=req.project_bank_name)
        hard.update(project_hard_rules(req.brief, req.project_bank_name))
    if project is not None:
        loaded[project.name] = project
    for k, v in (req.hard_rules or {}).items():
        if ":" not in k:
            raise HTTPException(422, f"hard_rules 的键要写成 题库名:题号，收到 {k!r}")
        bname, qid = k.split(":", 1)
        hard[(bname, qid)] = v
    client = _client()
    try:
        dj = _judge_draft(client, {"title": req.title, "body": req.body}, fq=fq, platform=platform, project=project, human=human,
                          judge_paras=req.judge_paras, hard_rules=hard, subject_id=req.subject_id, subject_type=req.subject_type)
    except JevError as exc:
        raise HTTPException(502, f"Jev 调用失败：{exc}")
    plan = repair_plan(dj, loaded, validated=set(req.validated or []), target=req.target)
    rows = []
    for bname, r in dj.results.items():
        rows.extend(ledger_rows(r, loaded[bname], run_tag=req.run_tag))
    written = _write_rows(rows) if req.write else None
    return {"subject_id": req.subject_id, "passed": dj.passed(), "profile": dj.profile, "hard_fails": dj.hard_fails,
            "ambiguous": dj.ambiguous, "para_stats": dj.para_stats, "plan": plan, "detail": dj.detail,
            "calls": dj.calls, "usage": dj.usage, "banks": {n: {"version": b.version, "sha256": b.sha256} for n, b in loaded.items()},
            "rows": len(rows), "ledger_rows": rows if req.return_rows else None, "written": written}
