# -*- coding: utf-8 -*-
"""给写手（Claude Code / WorkBuddy 里的模型）用的 MCP 工具：judge_draft · repair_plan_for · judge_comments · comment_repair_plan_for · judge_thread。

写作台的 deskcore 是流程纪律，这里是判定；写手的模型在自己的会话里调这几个工具，拿到题号 + 概率 + 证据句，
自己按修改单改稿，再判一遍——这就是「写后」参与点在写手这一侧的形态（deskcore 的 commit_drafts 挂 HTTP 判定是服务器侧的形态）。

启动（stdio）：python -m judge.mcp_server
Claude Code 的 .mcp.json：{"mcpServers": {"judge": {"command": "python", "args": ["-m", "judge.mcp_server"],
                                                     "env": {"TYPESAFE_API_KEY": "…", "JUDGE_PROJECT": "TUGE", "JUDGE_CATEGORY": "教育"}}}}

写手手里的都是未发布稿，所以每个工具都走数据出境规则（judge/policy.py，docs/00 #7）：项目代号从参数 project 或环境变量 JUDGE_PROJECT 取，
两处都没有就拒绝；处方药项目没清出境一律拒绝；项目层（brief）只有放行的项目才判。层的组装、硬约束、brief 编题与 HTTP /judge_draft
是同一段代码（judge/draft.py）。暗题（judge/hidden.py）不出现在任何工具的返回里。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import comments as CM
from . import policy as P
from .banks import discover, load_bank
from .draft import setup_draft
from .hidden import all_hidden, hidden_ids
from .jev_client import JevClient
from .loop import DEFAULT_HARD_RULES, judge_draft as _judge_draft, redact, repair_plan as _repair_plan

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


_CLIENT: Optional[JevClient] = None


def _client() -> JevClient:
    """整个 MCP 进程共用一个客户端：以前每次工具调用、每条评论都新建一个、重读一次密钥文件。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = JevClient(mock=os.environ.get("JUDGE_MOCK") == "1")
    return _CLIENT


DEFAULT_HARD = DEFAULT_HARD_RULES   # 与 HTTP /judge_draft 共用同一份默认硬约束（loop.DEFAULT_HARD_RULES）
DEFAULT_BANKS = ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.2"]


def _project(project: Optional[str], category: Optional[str]) -> tuple:
    return (project or os.environ.get("JUDGE_PROJECT") or None), (category or os.environ.get("JUDGE_CATEGORY") or None)


def _setup(banks: Optional[list], project: Optional[str], category: Optional[str], brief: Optional[dict], hard_rules: Optional[dict]):
    project, category = _project(project, category)
    return setup_draft(_bank, banks or DEFAULT_BANKS, subject_type="aw_version", project_code=project, category=category,
                       brief=brief, hard_rules=hard_rules)


def _check_comment_policy(project: Optional[str], category: Optional[str]) -> None:
    """评论工具判的是写手自己这篇还没发的帖子下的候选评论：同样按未发布稿过数据出境规则。"""
    project, category = _project(project, category)
    P.decide(["aw_version"], project, category)


@mcp.tool()
def list_banks() -> list:
    """列出可用题库（名字、版本、公开的题目 id；暗题只报个数）。"""
    out = []
    for n in discover(BANKS_DIR):
        ids = _bank(n).ids(); h = hidden_ids(n, ids)
        out.append({"name": n, "version": _bank(n).version, "questions": [q for q in ids if q not in h], "hidden": len(h)})
    return out


@mcp.tool()
def judge_draft(title: str, body: str, banks: Optional[list] = None, judge_paras: str = "on_fail", project: Optional[str] = None,
                category: Optional[str] = None, brief: Optional[dict] = None, hard_rules: Optional[dict] = None) -> dict:
    """判一篇稿子：默认跑特征题库 fq + 平台题库（大健康）+ 人感题库（篇级不过时再判段；过了也逐段判一遍不公开的检查，结果不回显）；给 brief 就按 brief 现编项目题库、
    brief 里的 P0 硬约束（want）接到判定（项目放行了项目层才跑）。project 不给就用环境变量 JUDGE_PROJECT。
    返回篇级画像 profile、硬伤 hard_fails（题、答案、概率、依据句）、没判出来的硬约束 unjudged、歧义题、段级分布 para_stats、
    数据出境 policy。不改稿。"""
    ds = _setup(banks, project, category, brief, hard_rules)
    hidden = all_hidden(ds.loaded)
    dj = _judge_draft(_client(), {"title": title, "body": body}, fq=ds.fq, platform=ds.platform, project=ds.project, human=ds.human,
                      judge_paras=judge_paras, hard_rules=ds.hard, hidden=hidden)
    view = redact(dj, hidden)
    return {**view, "calls": dj.calls, "policy": ds.decision.as_dict(), "ignored_banks": ds.ignored}


@mcp.tool()
def repair_plan_for(title: str, body: str, banks: Optional[list] = None, project: Optional[str] = None,
                    category: Optional[str] = None, brief: Optional[dict] = None, hard_rules: Optional[dict] = None) -> list:
    """判一篇稿子并给修改单：每条 = 题号 + 现在的答案与概率 + 依据句 + 该题定义。只说哪一句犯了哪条，不给改法。
    与 judge_draft 同样的层和硬约束（含 brief 编出的项目题库）。"""
    ds = _setup(banks, project, category, brief, hard_rules)
    hidden = all_hidden(ds.loaded)
    dj = _judge_draft(_client(), {"title": title, "body": body}, fq=ds.fq, platform=ds.platform, project=ds.project,
                      human=ds.human, judge_paras="on_fail", hard_rules=ds.hard, hidden=hidden)
    return _repair_plan(dj, ds.loaded, hidden=hidden)


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
def judge_comments(post_title: str, post_body: str, comments: list, kind: str = "product", points: Optional[list] = None,
                   project: Optional[str] = None, category: Optional[str] = None) -> list:
    """给一批候选评论跑读者侧评论题库（言语行为 / 点名品牌 / 接住帖子 / 细节 / 语域 / 读者用处 / 像不像安排的）+ 运营侧（意图 / 像不像脚本）。
    comments: [{"id": "...", "text": "..."}]；kind: product / agency / shop（题干里主体怎么称呼）；points: 帖子要点，不给就从正文取。
    背书体 / 文案腔 / 只有评价 / 像安排的 / 像脚本 / 漏答 会被标出来。不改评论。"""
    _check_comment_policy(project, category)
    reader, ops = _bank("comment_reader_v0.4"), _bank("comment_ops_v0.2")
    post = _post(post_title, post_body, kind, points); fill = CM.fill_for(post)
    out = []
    for c in comments:
        cj = CM.judge_comment(_client(), post, c["text"], None, reader, ops=ops, fill=fill, subject_id=str(c.get("id", "")))
        out.append({"id": c.get("id"), "flags": cj.flags, "needs_review": cj.needs_review,
                    "items": {k: {"answer": v.get("answer"), "p": v.get("p")} for k, v in cj.items.items()}})
    return out


@mcp.tool()
def comment_repair_plan_for(post_title: str, post_body: str, text: str, slot: dict, kind: str = "product",
                            points: Optional[list] = None, project: Optional[str] = None, category: Optional[str] = None) -> dict:
    """判一条候选评论是否对得上它的评论位，不对就给修改单。
    slot 例：{"id": "exp", "speech_act": ["补充经验"], "may_name_brand": false, "must_echo": true, "value_ok": ["判断依据"], "min_detail": "模糊"}
    或提问位 {"id": "q1", "speech_act": ["提问"], "must_echo": false, "value_ok": ["可行动信息","判断依据","无"], "min_detail": "无"}。
    返回 passed / hard_fails / flags / plan（每条 = 题号 + 现在的答案与概率 + 要改成什么 + 那个选项的定义）。不改评论。"""
    _check_comment_policy(project, category)
    reader, ops = _bank("comment_reader_v0.4"), _bank("comment_ops_v0.2")
    post = _post(post_title, post_body, kind, points); s = _slot(slot); fill = CM.fill_for(post)
    cj = CM.judge_comment(_client(), post, text, s, reader, ops=ops, fill=fill)
    return {"passed": cj.passed(), "needs_review": cj.needs_review, "flags": cj.flags, "hard_fails": cj.hard_fails,
            "profile": cj.profile(), "plan": CM.comment_repair_plan(cj, {"reader": reader, "ops": ops}, fill), "calls": cj.calls}


@mcp.tool()
def judge_thread(post_title: str, post_body: str, comments: list, kind: str = "product", points: Optional[list] = None,
                 max_named: int = 1, project: Optional[str] = None, category: Optional[str] = None) -> dict:
    """把一组评论当评论区整体判：夸的比例 / 同一句式 / 有没有摩擦（含贴主回复）/ 整体像不像安排的，外加代码算的点名条数、背书条数。
    四题任一不过都算不过（没有摩擦也算）；评论区题漏答、或任一条评论要复核（漏答 / 背书体），整组也不算过，needs_review=true。
    comments: [{"text": "...", "account"?: "...", "role"?: "贴主|读者位|运营", "reply_to_text"?: "..."}]。返回整体判定 + 每条的旗子。"""
    _check_comment_policy(project, category)
    reader, thread = _bank("comment_reader_v0.4"), _bank("comment_thread_v0.4")
    post = _post(post_title, post_body, kind, points); fill = CM.fill_for(post)
    judged = [CM.judge_comment(_client(), post, c["text"], None, reader, fill=fill, reply_to_text=c.get("reply_to_text", ""),
                               subject_id=str(i)) for i, c in enumerate(comments)]
    tj = CM.judge_thread(_client(), post, comments, thread, fill=fill, judged=judged, max_named=max_named)
    needs_review = tj.needs_review or any(cj.needs_review for cj in judged)   # 与 produce_comments 同口径（codex review on #2）
    return {"passed": tj.passed() and not needs_review, "needs_review": needs_review,
            "hard_fails": tj.hard_fails, "unjudged": tj.unjudged, "code": tj.code,
            "items": {q: {"answer": it.get("answer"), "p": it.get("p")} for q, it in tj.items.items()},
            "comments": [{"text": c["text"], "flags": cj.flags, "needs_review": cj.needs_review, "profile": cj.profile()}
                         for c, cj in zip(comments, judged)],
            "calls": tj.calls + sum(cj.calls for cj in judged)}


if __name__ == "__main__":
    mcp.run()
