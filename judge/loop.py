# -*- coding: utf-8 -*-
"""生产回路：Jev 参与写作的三个点（写前 · 写中 · 写后），这才是「Jev 进生产流程」的部分。

  写前  compile_project_bank(brief)      把 brief 编译成项目层题库：意图清单 → 「这段承担哪个意图（含无）」；
                                         能写成闭集的 P0 硬约束 → 是非题。生成端和判定端用同一套定义。
  写中  best_of_k(...)                   便宜模型出 k 个候选，逐个判，按硬伤数 + 目标分布距离选一个。
  写后  judge_draft(...) → repair_plan   篇级判（fq + 平台 + 项目题库）→ 不过再判段（人感题库）→ 修改指令 =
        repair(...)                      题号 + 概率 + 证据句 + 那道题的定义；改由便宜模型只改指定的句子，再判，直到过或预算用完。
  整条  produce(...)                     把上面串起来，返回终稿 + 每一步的判定轨迹（trail）+ 篇级画像（profile）。

生成端是可插拔的：任何「prompt → 文本」的可调用对象都行（Anthropic 兼容端点 / OpenAI 兼容端点 / 本地函数）。
本模块不写字；所有文字改动都由生成端做，Jev 只判。
修改指令里 fq 题的正向写法（aw_instruction）只对过了闸二的题下发（docs/28 §7）；闸二之前，指令只来自平台层和项目层的硬约束和人感题库。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from .banks import Bank, Question
from .core import apply_evidence, build_evidence_call, judge_note, judge_state
from .jev_client import JevClient

Generator = Callable[..., list]  # (prompt: str, n: int = 1) -> list[str]


# ── 写前：brief → 项目层题库 ────────────────────────────────────────────

def compile_project_bank(brief: dict, name: str = "project", model: str = "jev-1.13.0") -> Bank:
    """brief 形状（都可选）：
      intents:  [{"label": "烟瘾场景", "means": "…"}, …]   每段要承担的意图清单
      hard_rules: [{"id": "no_efficacy", "ask": "有没有承诺疗效？", "yes": "…", "no": "…", "want": false}, …]
      angle:    {"label": "健身房教练劝戒烟", "means": "…"}   本篇分到的切口 / 坐标
    """
    qs = []
    intents = brief.get("intents") or []
    if intents:
        crit = {i["label"]: i.get("means", "") or i["label"] for i in intents}
        crit["无"] = "这一段不承担任何营销意图，只是人设、场景、闲笔或情绪"
        qs.append(Question("intent_of_para", "choice", "这一段主要承担 brief 里的哪个意图？没有就选「无」。", crit,
                           unclear_labels=[], feeds="意图分布（「无」必须占一定比例）", on_ambiguous="进人工", ask="这一段承担哪个意图"))
    for r in brief.get("hard_rules") or []:
        qs.append(Question(r["id"], "noul", r["ask"], {"true": r.get("yes", "是"), "false": r.get("no", "否")},
                           evidence="on_true", feeds=f"P0 硬约束（want={r.get('want', False)}）", on_ambiguous="进人工", ask=r["ask"]))
    if brief.get("angle"):
        a = brief["angle"]
        qs.append(Question("on_angle", "noul", f"这篇稿子是不是按这个切口写的：{a['label']}（{a.get('means', '')}）？",
                           {"true": "主线就是这个切口", "false": "主线是别的，或者只沾了个边"},
                           feeds="切口对不对得上", on_ambiguous="进人工", ask="是不是按分到的切口写的"))
    return Bank(name=name, version=brief.get("version", "p-v1"), model=model, fmt="jev", questions=qs,
                ambiguity={"choice_top_min": 0.60, "choice_margin_min": 0.20, "noul_band": [0.35, 0.65]},
                sha256=hashlib.sha256(json.dumps(brief, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest())


# ── 生成端适配 ───────────────────────────────────────────────────────

class AnthropicCompatGenerator:
    """Anthropic Messages 兼容端点（三省六部走的中转站 / Kimi / DeepSeek 都这么接）。"""

    def __init__(self, model: str, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 max_tokens: int = 2000, temperature: float = 1.0, system: str = ""):
        self.model, self.max_tokens, self.temperature, self.system = model, max_tokens, temperature, system
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def __call__(self, prompt: str, n: int = 1) -> list:
        out = []
        for _ in range(n):
            body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                    "messages": [{"role": "user", "content": prompt}]}
            if self.system:
                body["system"] = self.system
            req = urllib.request.Request(self.base_url + "/v1/messages", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            out.append("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"))
        return out


class EchoGenerator:
    """测试用：候选 = prompt 里 ⟪…⟫ 括起来的文本；修补 = 按指令里的「删句」直接删。"""

    def __call__(self, prompt: str, n: int = 1) -> list:
        m = re.findall(r"⟪(.*?)⟫", prompt, flags=re.S)
        if m:
            return (m * n)[:n] if len(m) < n else m[:n]
        return [prompt] * n


# ── 写后：判 + 修改指令 + 修补 ─────────────────────────────────────────

@dataclass
class DraftJudgement:
    profile: dict = field(default_factory=dict)        # qid → answer（篇级，所有题库合并）
    detail: dict = field(default_factory=dict)         # bank → items
    results: dict = field(default_factory=dict)        # bank → 篇级 JudgeResult（落账本用；段级不落账本）
    hard_fails: list = field(default_factory=list)     # [(bank, qid, answer, p, evidence)]
    ambiguous: list = field(default_factory=list)      # [(bank, qid)]
    para_items: list = field(default_factory=list)     # 段级：[{idx, text, items}]
    para_stats: dict = field(default_factory=dict)     # 段级分布：语域 / 功能 / 摩擦 / 总结收尾
    usage: dict = field(default_factory=dict)
    calls: int = 0

    def passed(self) -> bool:
        return not self.hard_fails


def _para_split(body: str) -> list:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", body or "") if p.strip()]
    return [p for p in paras if not p.startswith("#")]


DEFAULT_HARD_RULES = {("platform_health_v0.1", "efficacy_claim"): "否", ("platform_health_v0.1", "medical_authority"): "否",
                      ("platform_health_v0.1", "fear_sell"): "否", ("feature_questions_v0_1", "efficacy_promise"): "否"}


def project_hard_rules(brief: dict, name: str = "project") -> dict:
    """brief.hard_rules[].want → {(题库名, 题号): 期望答案}，与 compile_project_bank 编出的题配套；
    want 可写 true/false 或「是」/「否」，不写按 false（答「是」即硬伤）。"""
    out = {}
    for r in brief.get("hard_rules") or []:
        w = r.get("want", False)
        out[(name, r["id"])] = w if isinstance(w, str) else ("是" if w else "否")
    return out


def judge_draft(client: JevClient, draft: dict, *, fq: Optional[Bank] = None, platform: Optional[Bank] = None,
                project: Optional[Bank] = None, human: Optional[Bank] = None, judge_paras: str = "on_fail",
                hard_rules: Optional[dict] = None, subject_id: str = "draft", subject_type: str = "aw_version") -> DraftJudgement:
    """draft = {"title": str, "body": str}。hard_rules = {(bank_name, qid): 期望答案}，答案不等于期望即硬伤。
    judge_paras: always / on_fail / never。subject_id 落账本时用（写作台传 versions.id）；段级用 subject_id:pN，不落账本。"""
    dj = DraftJudgement()
    title, body = draft.get("title") or "", draft.get("body") or ""
    raw = (f"标题：{title}\n正文：{body}") if title else body
    hard_rules = hard_rules or {}

    def _acc(r):
        dj.usage = {k: dj.usage.get(k, 0) + v for k, v in (r.usage or {}).items()} if r.usage else dj.usage
        dj.calls += r.calls

    if fq is not None:
        r = judge_note(client, fq, subject_id, raw, title_extraction="markers", with_evidence=True, subject_type=subject_type)
        dj.detail[fq.name] = r.items; dj.results[fq.name] = r; _acc(r)
    pseudo_spans = {"title": title, "body": body, "full": raw, "first_sentence": "", "last_para": ""}
    for bank in (platform, project):
        if bank is None:
            continue
        post_qids = [q.id for q in bank.questions if q.id != "intent_of_para"]
        if not post_qids:
            continue
        st = {"说明": "下面是一篇待发的小红书稿子。只根据文字判断。", "标题": title or "（无标题）", "正文": body}
        r = judge_state(client, bank, subject_id, st, subject_type=subject_type, qids=post_qids)
        ev = build_evidence_call(bank, pseudo_spans, r.items)
        if ev:
            ev_body, sent_map = ev; ev_body["model"] = bank.model
            apply_evidence(r.items, client.call(ev_body), sent_map); r.calls += 1
        dj.detail[bank.name] = r.items; dj.results[bank.name] = r; _acc(r)
    for bname, items in dj.detail.items():
        for qid, it in items.items():
            if it.get("answer") is None:
                continue
            dj.profile[qid] = it["answer"]
            if it.get("ambiguous"):
                dj.ambiguous.append((bname, qid))
            want = hard_rules.get((bname, qid))
            if want is not None and it["answer"] != want:
                dj.hard_fails.append((bname, qid, it["answer"], it.get("p"), it.get("evidence")))
    if human is not None and (judge_paras == "always" or (judge_paras == "on_fail" and dj.hard_fails)):
        paras = _para_split(body)
        intent_bank = project if (project is not None and "intent_of_para" in project.ids()) else None
        for i, p in enumerate(paras):
            st = {"说明": "下面是一篇小红书稿子里的一段。只看这一段。", "段落": p, "位置": f"第 {i + 1} 段，共 {len(paras)} 段"}
            r = judge_state(client, human, f"{subject_id}:p{i + 1}", st, subject_type=subject_type); _acc(r)
            items = dict(r.items)
            if intent_bank is not None:
                r2 = judge_state(client, intent_bank, f"{subject_id}:p{i + 1}", st, subject_type=subject_type, qids=["intent_of_para"]); _acc(r2)
                items.update(r2.items)
            dj.para_items.append({"idx": i + 1, "text": p, "items": items})
        dj.para_stats = para_stats(dj.para_items)
    return dj


def para_stats(para_items: list) -> dict:
    n = len(para_items) or 1
    reg = {}; fun = {}; friction = 0; close = 0; adj = 0
    for p in para_items:
        it = p["items"]
        r = it.get("para_register", {}).get("answer"); f = it.get("para_function", {}).get("answer")
        reg[r] = reg.get(r, 0) + 1; fun[f] = fun.get(f, 0) + 1
        friction += it.get("para_friction", {}).get("answer") == "是"
        close += it.get("para_summary_close", {}).get("answer") == "是"
        adj += it.get("para_adjective_pile", {}).get("answer") == "是"
    first_product = next((p["idx"] for p in para_items if p["items"].get("para_function", {}).get("answer") == "讲产品"), None)
    intents = {}
    for p in para_items:
        a = p["items"].get("intent_of_para", {}).get("answer")
        if a:
            intents[a] = intents.get(a, 0) + 1
    return {"n": n, "register": reg, "function": fun, "intents": intents, "no_intent_share": round(intents.get("无", 0) / n, 2) if intents else None,
            "no_product_share": round(1 - fun.get("讲产品", 0) / n, 2),
            "has_friction": friction > 0, "summary_close_paras": close, "adjective_pile_paras": adj,
            "first_product_para": first_product}


def repair_plan(dj: DraftJudgement, banks: dict, validated: Optional[set] = None,
                target: Optional[dict] = None) -> list:
    """修改指令 = 题号 + 概率 + 证据句 + 定义。不写改法，只说哪一句、犯了哪条、这条的定义是什么。
    validated：过了闸二的 fq 题 id，只有它们的 aw_instruction 会下发；target：篇级目标画像 {qid: 期望答案}。"""
    plan = []
    validated = validated or set()
    for bname, qid, ans, p, ev in dj.hard_fails:
        q = banks[bname].by_id().get(qid)
        want = "否" if ans == "是" else "是"
        plan.append({"kind": "hard", "bank": bname, "qid": qid, "now": ans, "want": want, "p": p, "evidence": ev,
                     "definition": (q.criteria.get("true") if ans == "是" else q.criteria.get("false")) if q and q.jtype == "noul" else (q.criteria.get(ans, "") if q else ""),
                     "instruction": f"「{q.ask if q else qid}」现在判「{ans}」（{p}），要改成「{want}」。" + (f"依据句：{ev}" if ev else "")})
    for qid, want in (target or {}).items():
        now = dj.profile.get(qid)
        if now is None or now == want:
            continue
        bname = next((b for b, items in dj.detail.items() if qid in items), None)
        q = banks[bname].by_id().get(qid) if bname else None
        if bname and q:
            plan.append({"kind": "target", "bank": bname, "qid": qid, "now": now, "want": want,
                         "p": dj.detail[bname][qid].get("p"), "evidence": dj.detail[bname][qid].get("evidence"),
                         "definition": q.criteria.get(want, ""),
                         "instruction": (f"「{q.ask}」现在是「{now}」，目标是「{want}」：{q.criteria.get(want, '')}") if qid in validated or bname != "feature_questions_v0_1"
                         else f"「{q.ask}」现在是「{now}」（目标「{want}」，此题未过闸二，只记录不下发）"})
    for p in dj.para_items:
        it = p["items"]
        if it.get("para_register", {}).get("answer") == "文案腔":
            plan.append({"kind": "para", "para": p["idx"], "qid": "para_register", "now": "文案腔", "want": "随手口语/认真分享",
                         "instruction": f"第 {p['idx']} 段读起来像文案（{it['para_register'].get('p')}）：{p['text'][:60]}…"})
        if it.get("para_summary_close", {}).get("answer") == "是":
            plan.append({"kind": "para", "para": p["idx"], "qid": "para_summary_close", "now": "是", "want": "否",
                         "instruction": f"第 {p['idx']} 段以总结或升华收尾，去掉最后那句归纳。"})
        if it.get("para_adjective_pile", {}).get("answer") == "是":
            plan.append({"kind": "para", "para": p["idx"], "qid": "para_adjective_pile", "now": "是", "want": "否",
                         "instruction": f"第 {p['idx']} 段有评价词堆叠，留一个、带上具体所指。"})
    return plan


def repair_prompt(draft: dict, plan: list) -> str:
    lines = ["下面是一篇小红书稿子和一张修改单。只改修改单指到的句子或段落，其余原样保留；不要加总结，不要加感叹，不要加新的评价词。",
             "输出格式：第一行「标题：…」，之后是正文。", "", f"标题：{draft.get('title', '')}", "正文：", draft.get("body", ""), "", "修改单："]
    for i, p in enumerate(plan, 1):
        lines.append(f"{i}. {p['instruction']}")
    return "\n".join(lines)


def parse_draft(text: str) -> dict:
    m = re.match(r"\s*标题[:：]\s*(.*?)\n(?:正文[:：]\s*)?(.*)$", text, flags=re.S)
    if m:
        return {"title": m.group(1).strip(), "body": m.group(2).strip()}
    return {"title": "", "body": text.strip()}


def repair(generator: Generator, draft: dict, plan: list) -> dict:
    if not plan:
        return draft
    out = generator(repair_prompt(draft, plan), n=1)[0]
    return parse_draft(out)


# ── 写中：best-of-k ────────────────────────────────────────────────────

def score(dj: DraftJudgement, target: Optional[dict] = None) -> float:
    """越小越好：硬伤每条 100，目标画像每处不符 10，歧义每处 1。"""
    s = 100.0 * len(dj.hard_fails) + 1.0 * len(dj.ambiguous)
    for qid, want in (target or {}).items():
        if dj.profile.get(qid) is not None and dj.profile[qid] != want:
            s += 10.0
    return s


def best_of_k(client: JevClient, generator: Generator, prompt: str, k: int, *, judge_kwargs: dict,
              target: Optional[dict] = None) -> tuple:
    cands = [parse_draft(t) for t in generator(prompt, n=k)]
    judged = [(c, judge_draft(client, c, **judge_kwargs)) for c in cands]
    judged.sort(key=lambda cj: score(cj[1], target))
    return judged[0], [(c, score(j, target)) for c, j in judged]


# ── 整条回路 ───────────────────────────────────────────────────────────

def produce(client: JevClient, generator: Generator, prompt: str, *, k: int = 3, max_repairs: int = 2,
            fq: Optional[Bank] = None, platform: Optional[Bank] = None, project: Optional[Bank] = None,
            human: Optional[Bank] = None, hard_rules: Optional[dict] = None, target: Optional[dict] = None,
            validated: Optional[set] = None) -> dict:
    banks = {b.name: b for b in (fq, platform, project, human) if b is not None}
    kw = dict(fq=fq, platform=platform, project=project, human=human, hard_rules=hard_rules, judge_paras="on_fail")
    (draft, dj), ranking = best_of_k(client, generator, prompt, k, judge_kwargs=kw, target=target)
    trail = [{"step": "best_of_k", "ranking": [s for _, s in ranking], "hard_fails": len(dj.hard_fails)}]
    rounds = 0
    while dj.hard_fails and rounds < max_repairs:
        plan = repair_plan(dj, banks, validated=validated, target=target)
        draft = repair(generator, draft, plan)
        dj = judge_draft(client, draft, **kw)
        rounds += 1
        trail.append({"step": f"repair_{rounds}", "plan": plan, "hard_fails": len(dj.hard_fails)})
    return {"draft": draft, "passed": dj.passed(), "profile": dj.profile, "para_stats": dj.para_stats,
            "hard_fails": dj.hard_fails, "ambiguous": dj.ambiguous, "trail": trail, "calls": dj.calls, "usage": dj.usage}
