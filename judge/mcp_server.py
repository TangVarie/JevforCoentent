# -*- coding: utf-8 -*-
"""给写手（Claude Code / WorkBuddy 里的模型）用的 MCP 工具——部署好的判定服务的**薄客户端**。

  薄客户端（走 HTTP）：judge_draft · repair_plan_for · list_banks
  本机 Jev（默认拒绝，JUDGE_ALLOW_LOCAL_JEV=1 才放行）：judge_comments · comment_repair_plan_for · judge_thread

2026-10-09 审计 A-09 之前这个模块在写手机器上进程内跑判定：.mcp.json 把 Jev 的 vendor 密钥（TYPESAFE_API_KEY）发到写手的环境里，
题库和暗题轮换也从本地 checkout 读，与 README / docs/31 §2.1「密钥只在这个服务的环境变量里」矛盾。现在稿子的判定走
POST {JUDGE_URL}/judge_draft、题库清单走 GET {JUDGE_URL}/banks，鉴权头 X-Judge-Key = JUDGE_API_KEY（与服务端同值）。
Jev 密钥不出服务端；题库、暗题（judge/hidden.py）、数据出境（judge/policy.py）都在服务端算，这里不读 banks/、不 import judge.hidden、
不建 JevClient。服务端返回的已经是抹掉暗题的视图（view / plan / recorded / policy / banks…），这里再把 ledger_rows 防御性去掉：
写手的模型看到的，就是服务端允许写手看到的那一份。

写作台的 deskcore 是流程纪律，这里是判定；写手的模型在自己的会话里调这几个工具，拿到题号 + 概率 + 证据句，
自己按修改单改稿，再判一遍——这就是「写后」参与点在写手这一侧的形态（deskcore 的 commit_drafts 挂 HTTP 判定是服务器侧的形态）。

启动（stdio）：python -m judge.mcp_server
Claude Code 的 .mcp.json：{"mcpServers": {"judge": {"command": "python", "args": ["-m", "judge.mcp_server"],
                                                     "env": {"JUDGE_URL": "https://judge.example.railway.app", "JUDGE_API_KEY": "…",
                                                             "JUDGE_PROJECT": "TUGE", "JUDGE_CATEGORY": "教育"}}}}

环境变量：JUDGE_URL（必需，服务地址）· JUDGE_API_KEY（必需，= 服务端的 JUDGE_API_KEY）· JUDGE_PROJECT / JUDGE_CATEGORY（默认项目代号 / 品类）·
JUDGE_HTTP_TIMEOUT_SEC（一次请求的超时，默认 180）· JUDGE_ALLOW_LOCAL_JEV=1（评论三个工具的本机路径，见下）。
两个必需的没配：启动时记一条警告，三个薄客户端工具返回 {"error": …, "missing": […]} 而不是抛异常。
429 / 5xx / 连不上按退避重试（最多 2 次，同 loop.AnthropicCompatGenerator）；401 / 403 / 422 等不重试，服务端的 detail 原样带回。

数据出境（judge/policy.py，docs/00 #7）：项目代号从参数 project 或环境变量 JUDGE_PROJECT 取，两处都没有就在本地拒绝（不发一个会 422 的请求）；
处方药项目拒绝、项目层只有放行的项目才判——这些由服务端 /judge_draft 执行并回显在响应的 policy 里，客户端只是把 project / category 传过去。

评论工具（judge_comments / comment_repair_plan_for / judge_thread）今天没有 HTTP 端点，仍是进程内调 Jev（要 TYPESAFE_API_KEY + 本地 banks/）。
写手机器不该持有 vendor 密钥，所以它们**默认拒绝**；只有合法持有密钥的内部 / 运维机器显式设 JUDGE_ALLOW_LOCAL_JEV=1 才走原来的进程内路径
（进程内的 import 都在工具函数里懒加载，薄客户端路径一个都不碰）。评论的 HTTP 端点另开 change，不在这里发明。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("judge.mcp_server")
mcp = FastMCP("judge")

DEFAULT_BANKS = ["feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.2"]   # 与 api.DEFAULT_DRAFT_BANKS 同
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}                      # 同 loop.AnthropicCompatGenerator
RETRIES = 2
RUN_TAG = "mcp"          # 只用来标账本行；MCP 永远 write=false，所以服务端不会真写
REQUIRED_ENV = ("JUDGE_URL", "JUDGE_API_KEY")


class ThinClientError(Exception):
    """工具回给写手的错误：dict 而不是异常（MCP 工具抛异常只会变成一句 isError，模型看不到为什么）。"""

    def __init__(self, error: str, **extra):
        super().__init__(error)
        self.error, self.extra = error, extra

    def as_dict(self) -> dict:
        return {"error": self.error, **self.extra}


# ── HTTP ────────────────────────────────────────────────────────────────

def _missing_env() -> list:
    return [n for n in REQUIRED_ENV if not os.environ.get(n, "").strip()]


def _config() -> tuple:
    missing = _missing_env()
    if missing:
        raise ThinClientError(f"没配 {' / '.join(missing)}：MCP 是判定服务的薄客户端，要知道服务地址和鉴权 key（写在 .mcp.json 的 env 里，见模块说明）",
                              missing=missing)
    return os.environ["JUDGE_URL"].strip().rstrip("/"), os.environ["JUDGE_API_KEY"].strip()


def _timeout() -> float:
    try:
        return float(os.environ.get("JUDGE_HTTP_TIMEOUT_SEC", "180"))
    except ValueError:
        return 180.0


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """服务端的 HTTPException 是 {"detail": "…"}；拿不到就回原文（截断）。"""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(exc.reason)
    try:
        d = json.loads(raw)
    except ValueError:
        return raw[:500] or str(exc.reason)
    if isinstance(d, dict) and "detail" in d:
        return d["detail"] if isinstance(d["detail"], str) else json.dumps(d["detail"], ensure_ascii=False)[:500]
    return raw[:500]


def _request(method: str, path: str, body: Optional[dict] = None):
    """一次 HTTP 调用：429 / 5xx / 连不上退避重试（最多 RETRIES 次）；其余状态码直接变成 ThinClientError（detail 原样带回）。"""
    base, key = _config()
    url = base + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"X-Judge-Key": key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last: Optional[ThinClientError] = None
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_timeout()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            last = ThinClientError(f"judge 服务回 {exc.code}：{detail}", status=exc.code, detail=detail, url=url)
            if exc.code not in RETRY_STATUSES or attempt >= RETRIES:
                raise last from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = ThinClientError(f"连不上 judge 服务（{url}）：{exc}", url=url)
            if attempt >= RETRIES:
                raise last from None
        time.sleep(min(0.5 * (2 ** attempt), 8.0))
    raise last  # pragma: no cover — 循环里要么 return 要么 raise


def _post_json(path: str, body: dict):
    return _request("POST", path, body)


def _get_json(path: str):
    return _request("GET", path)


# ── 稿子（薄客户端）────────────────────────────────────────────────────

def _project(project: Optional[str], category: Optional[str]) -> tuple:
    return (project or os.environ.get("JUDGE_PROJECT") or None), (category or os.environ.get("JUDGE_CATEGORY") or None)


def _draft_body(title: str, body: str, banks: Optional[list], judge_paras: str, project: Optional[str], category: Optional[str],
                brief: Optional[dict], hard_rules: Optional[dict], target: Optional[dict], validated: Optional[list],
                subject_id: str) -> dict:
    """与 api.DraftRequest 同形。project 两处都没有在本地就拒（服务端会 422，没必要发）；其余出境规则服务端执行。"""
    project, category = _project(project, category)
    if not project:
        raise ThinClientError("policy: 未发布稿必须带 project（参数 project 或环境变量 JUDGE_PROJECT）：不知道是哪个项目就判断不了能不能出境（docs/00 #7）")
    req = {"title": title, "body": body, "subject_id": subject_id or "draft", "subject_type": "aw_version",
           "project": project, "category": category, "banks": list(banks or DEFAULT_BANKS), "brief": brief, "hard_rules": hard_rules,
           "target": target, "validated": validated, "judge_paras": judge_paras, "run_tag": RUN_TAG, "write": False, "return_rows": False}
    return {k: v for k, v in req.items() if v is not None}


def _strip(d):
    """服务端已经抹掉暗题；账本行（含全部题）不该到写手手里，return_rows=false 之外再防御性去掉一次。"""
    if isinstance(d, dict):
        d.pop("ledger_rows", None)
    return d


@mcp.tool()
def list_banks() -> list:
    """列出判定服务上可用的题库（名字、版本、层、公开的题目 id；暗题只报个数）。GET {JUDGE_URL}/banks。出错时返回 {"error": …}。"""
    try:
        return _get_json("/banks")
    except ThinClientError as exc:
        return exc.as_dict()


@mcp.tool()
def judge_draft(title: str, body: str, banks: Optional[list] = None, judge_paras: str = "on_fail", project: Optional[str] = None,
                category: Optional[str] = None, brief: Optional[dict] = None, hard_rules: Optional[dict] = None,
                target: Optional[dict] = None, validated: Optional[list] = None, subject_id: str = "draft") -> dict:
    """判一篇稿子（POST {JUDGE_URL}/judge_draft）：默认跑特征题库 fq + 平台题库（大健康）+ 人感题库（篇级不过时再判段；过了也逐段判一遍不公开的检查，结果不回显）；
    给 brief 就按 brief 现编项目题库、brief 里的 P0 硬约束（want）接到判定（项目放行了项目层才跑）。project 不给就用环境变量 JUDGE_PROJECT，两处都没有就拒绝。
    返回服务端的响应原样：篇级画像 profile、硬伤 hard_fails（题、答案、概率、依据句）、没判出来的硬约束 unjudged、歧义题、段级分布 para_stats、
    修改单 plan、只记录的目标题 recorded、数据出境 policy、用的题库 banks。不改稿；不落账本。出错时返回 {"error": …, "status"?: …, "detail"?: …}。"""
    try:
        d = _post_json("/judge_draft", _draft_body(title, body, banks, judge_paras, project, category, brief, hard_rules, target, validated, subject_id))
    except ThinClientError as exc:
        return exc.as_dict()
    return _strip(d)


@mcp.tool()
def repair_plan_for(title: str, body: str, banks: Optional[list] = None, project: Optional[str] = None,
                    category: Optional[str] = None, brief: Optional[dict] = None, hard_rules: Optional[dict] = None,
                    target: Optional[dict] = None, validated: Optional[list] = None, subject_id: str = "draft") -> dict:
    """判一篇稿子并只拿修改单（同 judge_draft 的一次 /judge_draft 调用）：plan 每条 = 题号 + 现在的答案与概率 + 依据句 + 该题定义，只说哪一句犯了哪条，不给改法；
    recorded 是没过闸二、只记录不下发的目标题（给了 target 才有）。与 judge_draft 同样的层和硬约束（含 brief 编出的项目题库）。
    返回 {"plan": […], "recorded": […], "passed": …, "invalid_reason": …, "policy": …}；出错时返回 {"error": …}。"""
    try:
        d = _post_json("/judge_draft", _draft_body(title, body, banks, "on_fail", project, category, brief, hard_rules, target, validated, subject_id))
    except ThinClientError as exc:
        return exc.as_dict()
    if not isinstance(d, dict) or "plan" not in d:
        return {"error": "judge 服务的 /judge_draft 响应里没有 plan（服务端版本太旧，或不是 judge 服务？）",
                "keys": sorted(d) if isinstance(d, dict) else type(d).__name__}
    return {"plan": d["plan"], "recorded": d.get("recorded", []), "passed": d.get("passed"), "invalid_reason": d.get("invalid_reason"),
            "policy": d.get("policy"), "calls": d.get("calls")}


# ── 评论（本机 Jev：默认拒绝，JUDGE_ALLOW_LOCAL_JEV=1 才放行）─────────────

BANKS_DIR = Path(os.environ.get("JUDGE_BANKS_DIR", Path(__file__).resolve().parent.parent / "banks"))
_banks: dict = {}
_CLIENT = None


def _require_local_jev(tool: str) -> None:
    if os.environ.get("JUDGE_ALLOW_LOCAL_JEV") != "1":
        raise ThinClientError(f"{tool} 今天没有 HTTP 端点，只能在本机进程内调 Jev（要 TYPESAFE_API_KEY + 本地 banks/）；写手机器不该持有 vendor 密钥，所以默认拒绝。"
                              "合法持有密钥的内部 / 运维机器显式设 JUDGE_ALLOW_LOCAL_JEV=1 才放行。", tool=tool, local_jev_allowed=False)


def _bank(name: str):
    """本机路径才读题库：import 放在函数里，薄客户端路径不碰 banks/。"""
    from .banks import discover, load_bank
    if name not in _banks:
        found = discover(BANKS_DIR)
        if name not in found:
            raise ValueError(f"没有这个题库：{name}（有：{sorted(found)}）")
        _banks[name] = load_bank(found[name], name=name)
    return _banks[name]


def _client():
    """本机路径整个 MCP 进程共用一个 Jev 客户端（以前每次工具调用、每条评论都新建一个、重读一次密钥文件）。"""
    global _CLIENT
    if _CLIENT is None:
        from .jev_client import JevClient
        _CLIENT = JevClient(mock=os.environ.get("JUDGE_MOCK") == "1")
    return _CLIENT


def _check_comment_policy(project: Optional[str], category: Optional[str]) -> None:
    """评论工具判的是写手自己这篇还没发的帖子下的候选评论：同样按未发布稿过数据出境规则（本机路径，所以在这里算）。"""
    from . import policy as P
    project, category = _project(project, category)
    P.decide(["aw_version"], project, category)


def _post(post_title: str, post_body: str, kind: str, points: Optional[list]) -> dict:
    p = {"title": post_title, "body": post_body, "kind": kind}
    if points:
        p["points"] = list(points)
    return p


def _slot(slot: Optional[dict]):
    from . import comments as CM
    if not slot:
        return None
    return CM.slots_from_brief({"comment_slots": [slot]})[0]


@mcp.tool()
def judge_comments(post_title: str, post_body: str, comments: list, kind: str = "product", points: Optional[list] = None,
                   project: Optional[str] = None, category: Optional[str] = None) -> list:
    """给一批候选评论跑读者侧评论题库（言语行为 / 点名品牌 / 接住帖子 / 细节 / 语域 / 读者用处 / 像不像安排的）+ 运营侧（意图 / 像不像脚本）。
    comments: [{"id": "...", "text": "..."}]；kind: product / agency / shop（题干里主体怎么称呼）；points: 帖子要点，不给就从正文取。
    背书体 / 文案腔 / 只有评价 / 像安排的 / 像脚本 / 漏答 会被标出来。不改评论。
    【本机 Jev】没有 HTTP 端点，默认拒绝并返回 {"error": …}；只有设了 JUDGE_ALLOW_LOCAL_JEV=1 的内部机器才跑。"""
    try:
        _require_local_jev("judge_comments")
    except ThinClientError as exc:
        return exc.as_dict()
    from . import comments as CM
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
    返回 passed / hard_fails / flags / plan（每条 = 题号 + 现在的答案与概率 + 要改成什么 + 那个选项的定义）。不改评论。
    【本机 Jev】没有 HTTP 端点，默认拒绝并返回 {"error": …}；只有设了 JUDGE_ALLOW_LOCAL_JEV=1 的内部机器才跑。"""
    try:
        _require_local_jev("comment_repair_plan_for")
    except ThinClientError as exc:
        return exc.as_dict()
    from . import comments as CM
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
    comments: [{"text": "...", "account"?: "...", "role"?: "贴主|读者位|运营", "reply_to_text"?: "..."}]。返回整体判定 + 每条的旗子。
    【本机 Jev】没有 HTTP 端点，默认拒绝并返回 {"error": …}；只有设了 JUDGE_ALLOW_LOCAL_JEV=1 的内部机器才跑。"""
    try:
        _require_local_jev("judge_thread")
    except ThinClientError as exc:
        return exc.as_dict()
    from . import comments as CM
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


# ── 启动 ─────────────────────────────────────────────────────────────────

def _warn_if_unconfigured() -> None:
    """启动时只提醒一次；工具调用时再按次返回 {"error": …}。日志走 stderr（stdio MCP 的 stdout 是协议通道）。"""
    missing = _missing_env()
    if missing:
        log.warning("没配 %s：judge_draft / repair_plan_for / list_banks 会返回 {\"error\": …}。写在 .mcp.json 的 env 里（见 judge/mcp_server.py 模块说明）",
                    " / ".join(missing))


_warn_if_unconfigured()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    mcp.run()
