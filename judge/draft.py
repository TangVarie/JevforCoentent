# -*- coding: utf-8 -*-
"""一篇稿子要判哪几层、哪些硬约束：HTTP /judge_draft 与写手侧 MCP（judge_draft / repair_plan_for）共用这一处，
以前 MCP 用排除法猜项目题库、repair_plan_for 连 project 都没传、写手侧也送不进 brief，两个入口行为不一致（docs/02 §2 #3、§6）。

setup_draft(...) 负责：
  · 按名字分层（fq = 第一个 TV 格式；platform_* / human_feel_*；其余 Jev 格式的当项目题库，最多一份），认不出的回显在 ignored；
  · 项目题库来源：banks 里的名字、内联 project_bank、或 brief 现编（compile_project_bank + project_hard_rules）；
  · 数据出境（policy.decide）：处方药未发布稿直接 PolicyBlocked；项目层没放行时整层去掉，连同指向它的硬约束；
  · 硬约束：默认（只对真的加载了的层）+ brief 的 want + 调用方给的；调用方给的必须指到会判的题，指错 DraftSetupError（HTTP 422）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import policy as P
from .banks import Bank, check_bank, load_bank_data
from .loop import DEFAULT_HARD_RULES, compile_project_bank, project_hard_rules


class DraftSetupError(ValueError):
    """请求形状 / 组合不对：HTTP 映射成 422。"""


@dataclass
class DraftSetup:
    loaded: dict                                   # 这次真的会判的层 {name: Bank}
    fq: Optional[Bank] = None
    platform: Optional[Bank] = None
    human: Optional[Bank] = None
    project: Optional[Bank] = None
    hard: dict = field(default_factory=dict)       # {(bank, qid): 期望答案}
    ignored: list = field(default_factory=list)
    decision: Optional[P.Decision] = None


def norm_want(k: str, v) -> str:
    """硬约束的期望答案：JSON 布尔按 brief 同款口径转「是」/「否」；字符串原样（choice 题可以是选项名）。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, str) and v.strip():
        return v.strip()
    raise DraftSetupError(f"hard_rules[{k!r}] 的期望答案要是 true/false 或选项文字，收到 {v!r}")


def split_banks(names: list, get_bank: Callable[[str], Bank]) -> tuple:
    fq = platform = human = project = None
    loaded, ignored = {}, []
    for n in dict.fromkeys(names or []):          # 去重保序
        b = get_bank(n); loaded[n] = b
        if b.fmt == "tv" and fq is None:
            fq = b
        elif n.startswith("platform") and platform is None:
            platform = b
        elif n.startswith("human_feel") and human is None:
            human = b
        elif b.fmt == "jev" and P.layer_of(n) == "project" and project is None:
            project = b
        else:
            ignored.append(n); loaded.pop(n)
    return loaded, fq, platform, human, project, ignored


def project_from_request(loaded: dict, names: list, *, brief: Optional[dict], project_bank: Optional[dict],
                         project_bank_name: str) -> tuple:
    """内联 project_bank 或 brief 现编，二选一。返回 (bank | None, brief 的硬约束)。"""
    if project_bank is not None and brief is not None:
        raise DraftSetupError("project_bank 与 brief 只能给一个（brief 的 want 只能配 brief 编出的题）")
    if project_bank is None and brief is None:
        return None, {}
    if project_bank_name in loaded or project_bank_name in (names or []):
        raise DraftSetupError(f"project_bank_name={project_bank_name!r} 与 banks 里的题库同名，会把那一层的判定整层覆盖")
    if P.layer_of(project_bank_name) != "project":
        raise DraftSetupError(f"project_bank_name={project_bank_name!r} 撞上了通用层 / 平台层的名字前缀，换一个（默认 project）")
    try:
        if project_bank is not None:
            bank, hard = load_bank_data(project_bank, project_bank_name), {}
        else:
            bank, hard = compile_project_bank(brief, name=project_bank_name), project_hard_rules(brief, project_bank_name)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise DraftSetupError(f"项目题库 / brief 形状不对（{type(exc).__name__}: {exc}）；brief 要有 intents[].label、hard_rules[].id/ask、angle.label") from exc
    problems = check_bank(bank)
    if problems:
        raise DraftSetupError(f"项目题库有问题：{problems}")
    if not bank.questions:
        raise DraftSetupError("项目题库 / brief 一道题都没编出来（intents / hard_rules / angle 至少给一样），不能拿它当「判过了」")
    used = {qid: n for n, b in loaded.items() for qid in b.ids()}
    clash = [f"{q}（已在 {used[q]}）" for q in bank.ids() if q in used]
    if clash:
        raise DraftSetupError(f"项目题库的题号与已加载的层撞车：{clash}；profile 按题号合并，撞车会把两层的答案混在一起")
    return bank, hard


def setup_draft(get_bank: Callable[[str], Bank], names: list, *, subject_type: str = "aw_version",
                project_code: Optional[str] = None, category: Optional[str] = None, brief: Optional[dict] = None,
                project_bank: Optional[dict] = None, project_bank_name: str = "project",
                hard_rules: Optional[dict] = None) -> DraftSetup:
    """policy 的 PolicyBlocked / PolicyInputError 原样抛出（HTTP 分别映射 403 / 422）。
    一篇稿子一定是未发布稿：subject_type 只能是 aw_version / ssll_sample，否则调用方写个 note 就绕过了数据出境（codex review on #2）。"""
    if subject_type not in P.UNPUBLISHED_SUBJECT_TYPES:
        raise DraftSetupError(f"判稿的 subject_type 只能是 {sorted(P.UNPUBLISHED_SUBJECT_TYPES)}（未发布稿），收到 {subject_type!r}；公开笔记走 /judge")
    decision = P.decide([subject_type], project_code, category, published=False)
    loaded, fq, platform, human, project, ignored = split_banks(names, get_bank)
    inline, brief_hard = project_from_request(loaded, names, brief=brief, project_bank=project_bank,
                                              project_bank_name=project_bank_name)
    if inline is not None:
        if project is not None:
            raise DraftSetupError(f"banks 里已经有项目层题库 {project.name!r}，又给了 brief / project_bank：项目层只能有一份来源")
        project = inline; loaded[project.name] = project
    if project is not None and not decision.project_layer:
        decision.dropped_banks.append(project.name)
        loaded.pop(project.name, None)
        decision.dropped_hard_rules.extend(f"{b}:{q}" for (b, q) in brief_hard)
        brief_hard = {}
        project = None
    if fq is None and platform is None and human is None and project is None:
        extra = f"；数据出境规则去掉了项目层 {decision.dropped_banks}" if decision.dropped_banks else ""
        raise DraftSetupError(f"一份题库都没有（banks={names}，认不出层的：{ignored}{extra}）")
    if project is not None and set(project.ids()) <= {"intent_of_para"} and human is None:
        raise DraftSetupError("项目题库只有意图题（intent_of_para），它只在判段时问；没有人感题库这次一题都不会判")
    hard = {k: v for k, v in DEFAULT_HARD_RULES.items() if k[0] in loaded}
    hard.update(brief_hard)
    for k, v in (hard_rules or {}).items():
        if ":" not in k:
            raise DraftSetupError(f"hard_rules 的键要写成 题库名:题号，收到 {k!r}")
        bname, qid = k.split(":", 1)
        if bname in decision.dropped_banks:
            decision.dropped_hard_rules.append(k)      # 指向被数据出境规则去掉的项目层：跟着那一层一起去掉
            continue
        if bname not in loaded:
            raise DraftSetupError(f"hard_rules[{k!r}]：题库 {bname!r} 不在这次要判的层里（有：{sorted(loaded)}）")
        if qid not in loaded[bname].ids():
            raise DraftSetupError(f"hard_rules[{k!r}]：题库 {bname} 没有题 {qid!r}（有：{loaded[bname].ids()}）")
        hard[(bname, qid)] = norm_want(k, v)
    return DraftSetup(loaded=loaded, fq=fq, platform=platform, human=human, project=project, hard=hard,
                      ignored=ignored, decision=decision)
