"""Read-only candidate comment endpoints; policy/auth stay at the HTTP boundary."""
from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import comments as C
from .jev_client import JevError


class InputModel(BaseModel):
    model_config = {"extra": "forbid"}


class CommentSlot(InputModel):
    id: str = Field(min_length=1, max_length=80)
    speech_act: list[str] = Field(min_length=1, max_length=8)
    role: str = Field(default="读者位", max_length=40)
    reply_to: Optional[str] = Field(default=None, max_length=80)
    may_name_brand: bool = False
    must_echo: bool = True
    value_ok: list[str] = Field(default_factory=lambda: ["可行动信息", "判断依据"], min_length=1, max_length=8)
    min_detail: Literal["无", "模糊", "具体"] = "模糊"
    intent: Optional[str] = Field(default=None, max_length=40)
    note: str = Field(default="", max_length=500)


class CommentPost(InputModel):
    project: str = Field(min_length=1, max_length=100)
    category: Optional[str] = None
    post_title: str = Field(default="", max_length=500)
    post_body: str = Field(min_length=1, max_length=30000)
    kind: Literal["agency", "product", "shop"] = "product"
    points: Optional[list[str]] = Field(default=None, max_length=6)


class CommentRequest(CommentPost):
    text: str = Field(min_length=1, max_length=2000)
    slot: CommentSlot
    reply_to_text: str = Field(default="", max_length=2000)


class Candidate(InputModel):
    text: str = Field(min_length=1, max_length=2000)
    id: Optional[str] = Field(default=None, max_length=80)
    role: str = Field(default="读者位", max_length=40)
    account: str = Field(default="", max_length=80)
    reply_to_text: str = Field(default="", max_length=2000)
    slot: Optional[CommentSlot] = None


class ThreadRequest(CommentPost):
    comments: list[Candidate] = Field(min_length=1, max_length=20)
    max_named: int = Field(default=1, ge=0, le=20)


def _slot(value, reader, ops):
    if value is None:
        return None
    data = value.model_dump()
    for field, bank, qid in (("speech_act", reader, "speech_act"), ("value_ok", reader, "reader_value")):
        q = next(q for q in bank.questions if q.id == qid)
        if not set(data[field]) <= set(q.criteria):
            raise HTTPException(422, f"slot.{field} 不在题库闭集里")
    if data["intent"] is not None:
        q = next(q for q in ops.questions if q.id == "comment_intent")
        if data["intent"] not in q.criteria:
            raise HTTPException(422, "slot.intent 不在运营题库闭集里")
    return C.Slot(**data)


def _view(cj, banks, fill):
    needs_review = cj.needs_review or bool(cj.ambiguous)
    return {"passed": cj.passed() and not needs_review, "needs_review": needs_review,
            "profile": cj.profile(), "flags": cj.flags, "hard_fails": cj.hard_fails,
            "ambiguous": cj.ambiguous, "plan": C.comment_repair_plan(cj, banks, fill),
            "calls": cj.calls, "usage": cj.usage}


def attach(app, *, get_bank, get_client, policy, auth, parallel):
    def setup(req):
        # These endpoints inspect unpublished candidate comments; the caller cannot relax this.
        decision = policy(["comment"], req.project, req.category, False)
        if os.environ.get("JUDGE_COMMENT_TOOLS_ENABLED") != "1":
            raise HTTPException(503, "评论判定尚未开放，需完成评论题库的人工金标准验收")
        if not req.post_body.strip():
            raise HTTPException(422, "post_body 不能为空")
        post = {"title": req.post_title, "body": req.post_body[:600], "kind": req.kind, "points": req.points}
        reader, ops = get_bank("comment_reader_v0.4"), get_bank("comment_ops_v0.2")
        return decision, post, C.fill_for(post), reader, ops

    @app.post("/comment_repair_plan_for", dependencies=[Depends(auth)])
    def comment_repair_plan_for(req: CommentRequest):
        decision, post, fill, reader, ops = setup(req)
        if not req.text.strip():
            raise HTTPException(422, "text 不能为空")
        slot = _slot(req.slot, reader, ops)
        try:
            cj = C.judge_comment(get_client(), post, req.text, slot, reader, ops=ops, fill=fill,
                                 reply_to_text=req.reply_to_text)
        except JevError as exc:
            raise HTTPException(502, f"Jev 调用失败：{exc}")
        return {**_view(cj, {"reader": reader, "ops": ops}, fill), "policy": decision.as_dict()}

    @app.post("/judge_thread", dependencies=[Depends(auth)])
    def judge_thread(req: ThreadRequest):
        decision, post, fill, reader, ops = setup(req)
        if any(not c.text.strip() for c in req.comments):
            raise HTTPException(422, "评论 text 不能为空")
        slots = [_slot(c.slot, reader, ops) for c in req.comments]
        named_ids = [s.id for s in slots if s is not None]
        if len(named_ids) != len(set(named_ids)):
            raise HTTPException(422, "评论位 slot.id 不许重复")
        thread = get_bank("comment_thread_v0.4")
        client = get_client()
        try:
            judged = parallel(lambda pair: C.judge_comment(
                client, post, pair[0].text, pair[1], reader, ops=ops, fill=fill,
                reply_to_text=pair[0].reply_to_text, role=pair[0].role, account=pair[0].account), list(zip(req.comments, slots)))
            comments = [c.model_dump(exclude={"slot"}) for c in req.comments]
            tj = C.judge_thread(client, post, comments, thread, fill=fill, judged=judged, max_named=req.max_named)
        except JevError as exc:
            raise HTTPException(502, f"Jev 调用失败：{exc}")
        views = [_view(cj, {"reader": reader, "ops": ops}, fill) for cj in judged]
        ambiguous = [qid for qid, item in tj.items.items() if item.get("ambiguous")]
        thread_review = any(q.id in ambiguous and "进人工" in q.on_ambiguous for q in thread.questions)
        needs_review = tj.needs_review or thread_review or any(v["needs_review"] for v in views)
        hard_fails = tj.hard_fails or any(cj.hard_fails for cj in judged)
        plan = []
        if all(slots):
            plan = C.thread_repair_plan(tj, slots, judged)
        elif tj.hard_fails:
            plan = [{"slot": None, "why": "请提供每条评论的 slot 目标画像，再定位需要调整的评论位"}]
        return {"passed": tj.passed() and not needs_review and not hard_fails,
                "needs_review": needs_review, "hard_fails": tj.hard_fails, "unjudged": tj.unjudged,
                "profile": {q: item.get("answer") for q, item in tj.items.items()}, "code": tj.code,
                "plan": plan, "comments": views, "ambiguous": ambiguous,
                "calls": tj.calls + sum(cj.calls for cj in judged), "policy": decision.as_dict()}
