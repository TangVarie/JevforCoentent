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
修改指令里 fq 题的正向写法（aw_instruction）只对过了闸二的题下发（docs/28 §7）；闸二之前，指令只来自平台层和项目层的硬约束和人感题库：
没过闸二的 fq 目标题一条都不进修改单（只进 recorded，落轨迹），fq 硬伤只给依据句、不给题干和定义。
暗题（judge/hidden.py）照判照算，但修改单和给写手看的输出里只说「有一项不公开的检查没过」。
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
from .core import add_usage, apply_evidence, build_evidence_call, enforce_evidence, judge_note, judge_state
from .jev_client import JevClient
from . import spans as sp

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
    """Anthropic Messages 兼容端点（三省六部走的中转站 / Kimi / DeepSeek 都这么接）。
    base_url 末尾带不带 /v1 都行（同三省六部 agents/__init__.py 的剥法）；密钥按 ANTHROPIC_API_KEY → MOONSHOT_API_KEY 找；
    429 / 5xx / 529 与连不上按退避重试。"""

    RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}

    def __init__(self, model: str, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 max_tokens: int = 2000, temperature: float = 1.0, system: str = "", retries: int = 3, timeout: float = 180.0):
        self.model, self.max_tokens, self.temperature, self.system = model, max_tokens, temperature, system
        base = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
        self.base_url = base[:-3] if base.endswith("/v1") else base
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("MOONSHOT_API_KEY", "")
        self.retries, self.timeout = retries, timeout

    def _post(self, body: dict) -> dict:
        import time
        import urllib.error
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last: Exception = RuntimeError("未知错误")
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.base_url + "/v1/messages", data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in self.RETRY_STATUSES or attempt >= self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
                if attempt >= self.retries:
                    raise
            time.sleep(min(0.5 * (2 ** attempt), 8.0))
        raise last

    def __call__(self, prompt: str, n: int = 1) -> list:
        out = []
        for _ in range(n):
            body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                    "messages": [{"role": "user", "content": prompt}]}
            if self.system:
                body["system"] = self.system
            data = self._post(body)
            out.append("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"))
        return out


class EchoGenerator:
    """测试用：候选 = prompt 里 ⟪…⟫ 括起来的文本；修补 = 把修改单里「依据句：…」指到的句子从正文里删掉，其余原样返回。"""

    def __call__(self, prompt: str, n: int = 1) -> list:
        m = re.findall(r"⟪(.*?)⟫", prompt, flags=re.S)
        if m:
            return (m * n)[:n] if len(m) < n else m[:n]
        if "\n修改单：" in prompt:
            head, plan = prompt.split("\n修改单：", 1)
            dm = re.search(r"\n标题：(.*?)\n正文：\n(.*)$", head, flags=re.S)
            title, body = (dm.group(1), dm.group(2).rstrip("\n")) if dm else ("", head)
            for ev in re.findall(r"依据句：(.+?)(?:\n|$)", plan):
                body = body.replace(ev.strip(), "")
            return [f"标题：{title}\n{body.strip()}"] * n
        return [prompt] * n


# ── 写后：判 + 修改指令 + 修补 ─────────────────────────────────────────

@dataclass
class DraftJudgement:
    profile: dict = field(default_factory=dict)        # qid → answer（篇级，所有题库合并）
    detail: dict = field(default_factory=dict)         # bank → items
    results: dict = field(default_factory=dict)        # bank → 篇级 JudgeResult（落账本用；段级不落账本）
    wants: dict = field(default_factory=dict)          # {(bank, qid): 期望答案}，就是判这篇时用的 hard_rules；修改单按它说「要改成什么」
    hard_fails: list = field(default_factory=list)     # [(bank, qid, answer, p, evidence)]
    ambiguous: list = field(default_factory=list)      # [(bank, qid)]
    para_items: list = field(default_factory=list)     # 段级：[{idx, text, items}]
    para_stats: dict = field(default_factory=dict)     # 段级分布：语域 / 功能 / 摩擦 / 总结收尾
    usage: dict = field(default_factory=dict)
    calls: int = 0
    unjudged: list = field(default_factory=list)       # [(bank, qid, invalid_reason)]：有硬约束的题没判出有效答案（漏答 / 选项外），不能算过
    invalid_reason: Optional[str] = None               # 整篇没法判（正文太短）：text_too_short

    def passed(self) -> bool:
        return not self.hard_fails and not self.unjudged and self.invalid_reason is None


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
    dj.wants = dict(hard_rules)
    if sp.visible_len(body) < sp.MIN_BODY_CHARS:
        # 正文太短：fq 会按 text_too_short 全跳，平台 / 项目题库判出来的「过」也没有意义 —— 整篇记无效，不算过，一次调用都不发
        dj.invalid_reason = "text_too_short"
        return dj

    def _acc(r):
        dj.usage = add_usage(dj.usage, r.usage)
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
            ev_resp = client.call(ev_body)
            apply_evidence(r.items, ev_resp, sent_map); r.calls += 1
            r.usage = add_usage(r.usage, ev_resp.get("usage"))        # 证据调用的用量也算进去，成本估算不再偏低
        enforce_evidence(bank, r.items)
        dj.detail[bank.name] = r.items; dj.results[bank.name] = r; _acc(r)
    for (bname, qid), want in hard_rules.items():
        it = dj.detail.get(bname, {}).get(qid)
        if it is not None and it.get("answer") is None and it.get("invalid_reason") not in ("no_title", "text_too_short"):
            dj.unjudged.append((bname, qid, it.get("invalid_reason")))
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


HIDDEN_INSTRUCTION = "有一项不公开的检查没过"
PARA_FIELDS_BY_QID = {"para_register": ("register",), "para_function": ("function", "no_product_share", "first_product_para"),
                      "para_friction": ("has_friction",), "para_summary_close": ("summary_close_paras",),
                      "para_adjective_pile": ("adjective_pile_paras",)}


def _is_fq(bank: Optional[Bank]) -> bool:
    return bank is not None and bank.fmt == "tv"          # 按格式认 fq，不按写死的名字（换名加载也守得住闸二）


def repair_plan(dj: DraftJudgement, banks: dict, validated: Optional[set] = None,
                target: Optional[dict] = None, hidden: Optional[set] = None, recorded: Optional[list] = None) -> list:
    """修改指令 = 题号 + 概率 + 证据句 + 定义。不写改法，只说哪一句、犯了哪条、这条的定义是什么。
    validated：过了闸二的 fq 题 id。没过闸二的 fq 题：目标画像不进修改单（只追加进 recorded，给轨迹 / 响应记录），
    硬伤只给依据句、不给题干和定义（通用层的题不原样塞进生成端的 prompt，docs/31 §2.2）。
    hidden：暗题 id（judge/hidden.py）；它们的条目只说「有一项不公开的检查没过」+ 段号 / 依据句。"""
    plan = []
    validated = validated or set()
    hidden = hidden or set()
    for bname, qid, ans, p, ev in dj.hard_fails:
        bank = banks.get(bname)
        q = bank.by_id().get(qid) if bank else None
        want = dj.wants.get((bname, qid)) or ("否" if ans == "是" else "是")
        if qid in hidden:
            plan.append({"kind": "hard", "bank": bname, "qid": "hidden", "now": None, "want": None, "p": p, "evidence": ev,
                         "definition": "", "instruction": HIDDEN_INSTRUCTION + (f"。依据句：{ev}" if ev else "")})
            continue
        if _is_fq(bank) and qid not in validated:
            plan.append({"kind": "hard", "bank": bname, "qid": qid, "now": ans, "want": want, "p": p, "evidence": ev,
                         "definition": "", "instruction": f"有一条合规硬约束没过（{p}）" + (f"。依据句：{ev}" if ev else "")})
            continue
        if q is None:
            definition = ""
        elif q.jtype == "noul":
            definition = q.criteria.get("true" if want == "是" else "false", "")
        else:
            definition = q.criteria.get(want, "")
        plan.append({"kind": "hard", "bank": bname, "qid": qid, "now": ans, "want": want, "p": p, "evidence": ev,
                     "definition": definition,
                     "instruction": f"「{q.ask if q else qid}」现在判「{ans}」（{p}），要改成「{want}」" + (f"：{definition}" if definition else "")
                                    + (f"。依据句：{ev}" if ev else "")})
    for qid, want in (target or {}).items():
        now = dj.profile.get(qid)
        if now is None or now == want or qid in hidden:
            continue
        bname = next((b for b, items in dj.detail.items() if qid in items), None)
        bank = banks.get(bname) if bname else None
        q = bank.by_id().get(qid) if bank else None
        if not (bname and q):
            continue
        entry = {"kind": "target", "bank": bname, "qid": qid, "now": now, "want": want,
                 "p": dj.detail[bname][qid].get("p"), "evidence": dj.detail[bname][qid].get("evidence")}
        if _is_fq(bank) and qid not in validated:
            if recorded is not None:
                recorded.append(dict(entry, why="未过闸二：只记录，不进修改单、不下发生成端"))
            continue
        plan.append(dict(entry, definition=q.criteria.get(want, ""),
                         instruction=f"「{q.ask}」现在是「{now}」，目标是「{want}」：{q.criteria.get(want, '')}"))
    for p in dj.para_items:
        it = p["items"]
        checks = (("para_register", it.get("para_register", {}).get("answer") == "文案腔", "文案腔", "随手口语/认真分享",
                   f"第 {p['idx']} 段读起来像文案（{it.get('para_register', {}).get('p')}）：{p['text'][:60]}…"),
                  ("para_summary_close", it.get("para_summary_close", {}).get("answer") == "是", "是", "否",
                   f"第 {p['idx']} 段以总结或升华收尾，去掉最后那句归纳。"),
                  ("para_adjective_pile", it.get("para_adjective_pile", {}).get("answer") == "是", "是", "否",
                   f"第 {p['idx']} 段有评价词堆叠，留一个、带上具体所指。"))
        for qid, hit, now, want, instr in checks:
            if not hit:
                continue
            if qid in hidden:
                plan.append({"kind": "para", "para": p["idx"], "qid": "hidden", "now": None, "want": None,
                             "instruction": f"第 {p['idx']} 段{HIDDEN_INSTRUCTION}：{p['text'][:60]}…"})
            else:
                plan.append({"kind": "para", "para": p["idx"], "qid": qid, "now": now, "want": want, "instruction": instr})
    return plan


def redact(dj: DraftJudgement, hidden: set) -> dict:
    """给写手 / 生成端看的判定视图：去掉暗题的题号、答案与由它算出来的段级分布字段。账本行不经过这里。"""
    def strip(items: dict) -> dict:
        return {q: v for q, v in items.items() if q not in hidden}
    stats = dict(dj.para_stats)
    for qid, fields_ in PARA_FIELDS_BY_QID.items():
        if qid in hidden:
            for f in fields_:
                stats.pop(f, None)
    return {"passed": dj.passed(), "invalid_reason": dj.invalid_reason,
            "profile": {q: a for q, a in dj.profile.items() if q not in hidden},
            "hard_fails": [(b, "hidden" if q in hidden else q, None if q in hidden else a, pp, ev) for b, q, a, pp, ev in dj.hard_fails],
            "unjudged": [(b, "hidden" if q in hidden else q, r) for b, q, r in dj.unjudged],
            "ambiguous": [(b, q) for b, q in dj.ambiguous if q not in hidden],
            "para_stats": stats,
            "detail": {b: strip(items) for b, items in dj.detail.items()},
            "paras": [{"idx": p["idx"], "items": strip(p["items"])} for p in dj.para_items]}


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
            validated: Optional[set] = None, hidden: Optional[set] = None) -> dict:
    from .hidden import all_hidden
    banks = {b.name: b for b in (fq, platform, project, human) if b is not None}
    hidden = all_hidden(banks) if hidden is None else hidden
    kw = dict(fq=fq, platform=platform, project=project, human=human, hard_rules=hard_rules, judge_paras="on_fail")
    (draft, dj), ranking = best_of_k(client, generator, prompt, k, judge_kwargs=kw, target=target)
    trail = [{"step": "best_of_k", "ranking": [s for _, s in ranking], "hard_fails": len(dj.hard_fails)}]
    rounds = 0
    while dj.hard_fails and rounds < max_repairs:
        recorded: list = []
        plan = repair_plan(dj, banks, validated=validated, target=target, hidden=hidden, recorded=recorded)
        draft = repair(generator, draft, plan)
        dj = judge_draft(client, draft, **kw)
        rounds += 1
        trail.append({"step": f"repair_{rounds}", "plan": plan, "recorded": recorded, "hard_fails": len(dj.hard_fails)})
    return {"draft": draft, "passed": dj.passed(), "profile": dj.profile, "para_stats": dj.para_stats,
            "hard_fails": dj.hard_fails, "ambiguous": dj.ambiguous, "trail": trail, "calls": dj.calls, "usage": dj.usage}
