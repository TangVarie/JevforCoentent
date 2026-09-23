# -*- coding: utf-8 -*-
"""给写手（Claude Code / WorkBuddy 里的模型）用的 MCP 工具：judge_draft · repair_plan_for · judge_comments · comment_repair_plan_for · judge_thread。

写作台的 deskcore 是流程纪律，这里是判定；写手的模型在自己的会话里调这几个工具，拿到题号 + 概率 + 证据句，
自己按修改单改稿，再判一遍——这就是「写后」参与点在写手这一侧的形态（deskcore 的 commit_drafts 挂 HTTP 判定是服务器侧的形态）。

启动（stdio）：python -m judge.mcp_server
Claude Code 的 .mcp.json：{"mcpServers": {"judge": {"command": "python", "args": ["-m", "judge.mcp_server"], "env": {"TYPESAFE_API_KEY": "…"}}}}
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import comments as CM
from .banks import discover, load_bank
from .jev_client import JevClient
from .loop import DEFAULT_HARD_RULES, judge_draft as _judge_draft, repair_plan as _repair_plan

BANKS_DIR = Path(os.environ.get("JUDGE_BANKS_DIR", Path(__file__).resolve().parent.parent / "banks"))
mcp = FastMCP("judge")
_banks: dict = {}


def _bank(name: str):
    if name not in _banks:
        found = discover(BANKS_DIR)
        if name not in found:
            raise ValueError(f"没有这个题库：{name}（有：{sorted(found)}）")
        _banks[name] = load_bank(found[name], name=name)
    return _banks[name]


def _client() -> JevClient:
    return JevClient(mock=os.environ.get("JUDGE_MOCK") == "1")


DEFAULT_HARD = DEFAULT_HARD_RULES   # 与 HTTP /judge_draft 共用同一份默认硬约束（loop.DEFAULT_HARD_RULES）


@mcp.tool()
def list_banks() -> list:
    """列出可用题库（名字、版本、题目 id）。"""
    return [{"name": n, "version": _bank(n).version, "questions": _bank(n).ids()} for n in discover(BANKS_DIR)]


@mcp.tool()
def judge_draft(title: str, body: str, banks: Optional[list] = None, judge_paras: str = "on_fail") -> dict:
    """判一篇稿子：默认跑特征题库 fq + 平台题库（大健康）+ 人感题库（篇级不过时再判段）。
    返回篇级画像 profile、硬伤 hard_fails（题、答案、概率、依据句）、歧义题、段级分布 para_stats。不改稿。"""
    banks = banks or ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.1"]
    loaded = {n: _bank(n) for n in banks}
    fq = next((b for b in loaded.values() if b.fmt == "tv"), None)
    platform = loaded.get("platform_health_v0.1")
    human = loaded.get("human_feel_para_v0.1")
    project = next((b for n, b in loaded.items() if n not in ("platform_health_v0.1", "human_feel_para_v0.1") and b.fmt == "jev" and not n.startswith("comment")), None)
    dj = _judge_draft(_client(), {"title": title, "body": body}, fq=fq, platform=platform, project=project, human=human,
                      judge_paras=judge_paras, hard_rules=DEFAULT_HARD)
    return {"passed": dj.passed(), "profile": dj.profile, "hard_fails": dj.hard_fails, "ambiguous": dj.ambiguous,
            "para_stats": dj.para_stats, "detail": dj.detail, "calls": dj.calls}


@mcp.tool()
def repair_plan_for(title: str, body: str, banks: Optional[list] = None) -> list:
    """判一篇稿子并给修改单：每条 = 题号 + 现在的答案与概率 + 依据句 + 该题定义。只说哪一句犯了哪条，不给改法。"""
    banks = banks or ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.1"]
    loaded = {n: _bank(n) for n in banks}
    fq = next((b for b in loaded.values() if b.fmt == "tv"), None)
    dj = _judge_draft(_client(), {"title": title, "body": body}, fq=fq, platform=loaded.get("platform_health_v0.1"),
                      human=loaded.get("human_feel_para_v0.1"), judge_paras="on_fail", hard_rules=DEFAULT_HARD)
    return _repair_plan(dj, loaded)


def _post(post_title: str, post_body: str, kind: str, points: Optional[list]) -> dict:
    p = {"title": post_title, "body": post_body, "kind": kind}
    if points:
        p["points"] = list(points)
    return p


def _slot(slot: Optional[dict]) -> Optional[CM.Slot]:
    if not slot:
        return None
    return CM.slots_from_brief({"comment_slots": [slot]})[0]


@mcp.tool()
def judge_comments(post_title: str, post_body: str, comments: list, kind: str = "product", points: Optional[list] = None) -> list:
    """给一批候选评论跑读者侧评论题库（言语行为 / 点名品牌 / 接住帖子 / 细节 / 语域 / 读者用处 / 像不像安排的）+ 运营侧（意图 / 像不像脚本）。
    comments: [{"id": "...", "text": "..."}]；kind: product / agency / shop（题干里主体怎么称呼）；points: 帖子要点，不给就从正文取。
    背书体 / 文案腔 / 只有评价 / 像安排的 / 像脚本 会被标出来。不改评论。"""
    reader, ops = _bank("comment_reader_v0.4"), _bank("comment_ops_v0.1")
    post = _post(post_title, post_body, kind, points); fill = CM.fill_for(post)
    out = []
    for c in comments:
        cj = CM.judge_comment(_client(), post, c["text"], None, reader, ops=ops, fill=fill, subject_id=str(c.get("id", "")))
        out.append({"id": c.get("id"), "flags": cj.flags, "needs_review": cj.needs_review,
                    "items": {k: {"answer": v.get("answer"), "p": v.get("p")} for k, v in cj.items.items()}})
    return out


@mcp.tool()
def comment_repair_plan_for(post_title: str, post_body: str, text: str, slot: dict, kind: str = "product",
                            points: Optional[list] = None) -> dict:
    """判一条候选评论是否对得上它的评论位，不对就给修改单。
    slot 例：{"id": "exp", "speech_act": ["补充经验"], "may_name_brand": false, "must_echo": true, "value_ok": ["判断依据"], "min_detail": "模糊"}
    或提问位 {"id": "q1", "speech_act": ["提问"], "must_echo": false, "value_ok": ["可行动信息","判断依据","无"], "min_detail": "无"}。
    返回 passed / hard_fails / flags / plan（每条 = 题号 + 现在的答案与概率 + 要改成什么 + 那个选项的定义）。不改评论。"""
    reader, ops = _bank("comment_reader_v0.4"), _bank("comment_ops_v0.1")
    post = _post(post_title, post_body, kind, points); s = _slot(slot); fill = CM.fill_for(post)
    cj = CM.judge_comment(_client(), post, text, s, reader, ops=ops, fill=fill)
    return {"passed": cj.passed(), "needs_review": cj.needs_review, "flags": cj.flags, "hard_fails": cj.hard_fails,
            "profile": cj.profile(), "plan": CM.comment_repair_plan(cj, {"reader": reader, "ops": ops}, fill), "calls": cj.calls}


@mcp.tool()
def judge_thread(post_title: str, post_body: str, comments: list, kind: str = "product", points: Optional[list] = None,
                 max_named: int = 1) -> dict:
    """把一组评论当评论区整体判：夸的比例 / 同一句式 / 有没有摩擦（含贴主回复）/ 整体像不像安排的，外加代码算的点名条数、背书条数。
    comments: [{"text": "...", "account"?: "...", "role"?: "贴主|读者位|运营", "reply_to_text"?: "..."}]。返回整体判定 + 每条的旗子。"""
    reader, thread = _bank("comment_reader_v0.4"), _bank("comment_thread_v0.3")
    post = _post(post_title, post_body, kind, points); fill = CM.fill_for(post)
    judged = [CM.judge_comment(_client(), post, c["text"], None, reader, fill=fill, reply_to_text=c.get("reply_to_text", ""),
                               subject_id=str(i)) for i, c in enumerate(comments)]
    tj = CM.judge_thread(_client(), post, comments, thread, fill=fill, judged=judged, max_named=max_named)
    return {"passed": tj.passed(), "hard_fails": tj.hard_fails, "code": tj.code,
            "items": {q: {"answer": it.get("answer"), "p": it.get("p")} for q, it in tj.items.items()},
            "comments": [{"text": c["text"], "flags": cj.flags, "profile": cj.profile()} for c, cj in zip(comments, judged)],
            "calls": tj.calls + sum(cj.calls for cj in judged)}


if __name__ == "__main__":
    mcp.run()
