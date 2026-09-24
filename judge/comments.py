# -*- coding: utf-8 -*-
"""评论生产回路：和 loop.py 同构，对象从「一篇稿子」换成「一条评论」和「一组评论」。

  写前  Slot · default_slots()             评论位：这一位要做什么（提问 / 补充经验 / 贴主回复答疑 …）、能不能点名、
                                           要不要接住帖子、读者从它那里要拿到什么。位就是目标画像；判定端和生成端用同一套定义。
  写中  best_of_k_comment()                生成端出 k 条候选，逐条判（读者侧 7 题 + 运营侧 2 题），按硬伤数排序取一条。
  写后  judge_comment() → comment_repair_plan() → repair_comment()
                                           修改单每条 = 题号 + 现在的答案与概率 + 要改成什么 + 那个选项的定义。改由生成端做。
  成组  judge_thread() · thread_repair_plan()
                                           一组评论选定后整体过评论区 4 题（夸的比例 / 同一句式 / 有没有摩擦 / 整体像不像安排的），
                                           四题任一不过就指出哪一位要换、为什么（docs/01 §4「不过就换位」，摩擦那题也算）；代码另算点名条数。
                                           换谁都修不了的（问题出在评论位配置本身）不换位，只在修改单里写明，不白烧轮次。
  整条  produce_comments()                 逐位 best-of-k + 修补 → 成组判 → 换位重出 → 返回评论组、每条画像、评论区画像、轨迹。

生成端可插拔（同 loop.Generator：prompt, n → [text]）；每一位的生成 prompt 由调用方的 prompt_for(post, slot) 给。
本模块不写字，只判、只出修改单。默认评论位里没有「亲历背书 / 旁观推荐」：读者侧题库把它们直接路由进高风险复核（feeds 写明），
一组里若 brief 硬要放这样的位，产出会带 needs_review。评论区的营销职能是承接（回答、补充、指路），不是背书（docs/31 §6.3）。

题干里按篇变的部分（帖子要点、主体称呼）通过 fill 填进题库 v0.4 / v0.3 的占位符；同一篇帖子下所有候选用同一份 fill。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from . import spans as sp
from .banks import Bank, fill_text, unfilled
from .core import add_usage, judge_state
from .jev_client import JevClient

Generator = Callable[..., list]

ENDORSE = ("亲历背书", "旁观推荐")
AGENCY_FILL = {"subject": "这家机构", "arranger": "机构", "decide": "找机构", "praised": "机构", "helper": "机构/老师"}
PRODUCT_FILL = {"subject": "这个产品", "arranger": "商家", "decide": "买", "praised": "这个产品", "helper": "这个产品"}
SHOP_FILL = {"subject": "这家店", "arranger": "店家", "decide": "去", "praised": "这家店", "helper": "店家"}
KIND_FILL = {"agency": AGENCY_FILL, "product": PRODUCT_FILL, "shop": SHOP_FILL}


# ── 写前：帖子要点与评论位 ───────────────────────────────────────────────

def post_points(body: str, n: int = 6, max_chars: int = 24) -> list:
    """从正文取要点：按句切，去掉太短的，每条截到 max_chars 字。brief 里给了 points 就不用这个。"""
    pts = []
    for s in sp.split_sentences(body or ""):
        s = re.sub(r"[\s。！？!?，,；;：:\"“”]+$", "", s.strip())
        s = re.sub(r"^[\s#＃]+", "", s)
        if len(s) < 6:
            continue
        pts.append(s[:max_chars])
        if len(pts) >= n:
            break
    return pts


def fill_for(post: dict) -> dict:
    """post = {"title", "body", "kind"?: agency|product|shop, "subject"?, "arranger"?, "decide"?, "helper"?, "points"?: [..]}"""
    fill = dict(KIND_FILL.get(post.get("kind") or "agency", AGENCY_FILL))
    for k in ("subject", "arranger", "decide", "praised", "helper"):
        if post.get(k):
            fill[k] = post[k]
    pts = list(post.get("points") or post_points(post.get("body", "")))
    fill["post_points"] = "、".join(pts) if pts else "（正文太短，没有可接的要点）"
    fill["post_examples"] = "".join(f"「{p}」" for p in pts[:3]) if pts else "「…」"
    return fill


@dataclass
class Slot:
    """一个评论位 = 这条评论的目标画像。默认值是最严的一档；提问位放宽 must_echo / value_ok。"""
    id: str
    speech_act: list                                  # 允许的言语行为（读者侧 speech_act 的选项名）
    role: str = "读者位"                               # 贴主 / 读者位 / 运营
    reply_to: Optional[str] = None                    # 回复哪一位（slot id），贴主回复答疑用
    may_name_brand: bool = False                      # names_brand 允许为「是」
    must_echo: bool = True                            # echoes_post 必须为「是」
    value_ok: list = field(default_factory=lambda: ["可行动信息", "判断依据"])   # reader_value 允许值
    min_detail: str = "模糊"                           # detail_level 至少到：无 / 模糊 / 具体
    intent: Optional[str] = None                      # 运营侧 comment_intent 期望值（有 ops 题库时才判）
    note: str = ""                                    # 给生成端的备注，本模块只原样传给 prompt_for

    def wants(self) -> dict:
        return {"speech_act": self.speech_act, "names_brand": "是" if self.may_name_brand else "否",
                "echoes_post": "是" if self.must_echo else "不限", "reader_value": self.value_ok,
                "register": ["随手口语", "认真分享"], "detail_level": f"≥{self.min_detail}", "intent": self.intent}


def default_slots(n_questions: int = 2, with_author_reply: bool = True, with_experience: bool = True) -> list:
    """默认一组：提问 ×n（读者位，不点名，不强求接帖）· 补充经验 ×1（读者位，不点名，要接帖、要有情境）·
    贴主回复答疑 ×1（回第一条提问，可点名，要给可行动信息）。有作者回复，评论区 has_friction 就有了着落，不用造质疑。"""
    slots = []
    for i in range(n_questions):
        slots.append(Slot(f"q{i + 1}", ["提问"], must_echo=False, value_ok=["可行动信息", "判断依据", "无"], min_detail="无",
                          intent="其他", note="问一件帖子没写清的事"))
    if with_experience:
        slots.append(Slot("exp", ["补充经验"], intent="补充信息", note="讲一段自己相关的事，不对主体下结论"))
    if with_author_reply and n_questions:
        slots.append(Slot("reply", ["回复答疑"], role="贴主", reply_to="q1", may_name_brand=True, must_echo=False,
                          value_ok=["可行动信息"], min_detail="无", intent="补充信息", note="贴主回答第一条提问，给能用的信息"))
    return slots


def slots_from_brief(brief: dict) -> list:
    """brief["comment_slots"] = [{"id", "speech_act": [...], "role"?, "reply_to"?, "may_name_brand"?, "must_echo"?, "value_ok"?, "min_detail"?, "intent"?, "note"?}]"""
    out = []
    for s in brief.get("comment_slots") or []:
        out.append(Slot(id=s["id"], speech_act=list(s["speech_act"]), role=s.get("role", "读者位"), reply_to=s.get("reply_to"),
                        may_name_brand=bool(s.get("may_name_brand", False)), must_echo=bool(s.get("must_echo", True)),
                        value_ok=list(s.get("value_ok") or ["可行动信息", "判断依据"]), min_detail=s.get("min_detail", "模糊"),
                        intent=s.get("intent"), note=s.get("note", "")))
    return out or default_slots()


# ── 写后：判一条 + 修改单 ────────────────────────────────────────────────

@dataclass
class CommentJudgement:
    text: str
    items: dict = field(default_factory=dict)          # 读者侧 7 题（+ 运营侧 2 题）→ {answer, p, ambiguous, ...}
    hard_fails: list = field(default_factory=list)     # [(qid, now, want, p)]
    flags: list = field(default_factory=list)          # 背书体 / 文案腔 / 只有评价 / 像安排的 / 像脚本
    ambiguous: list = field(default_factory=list)
    needs_review: bool = False                         # 背书体：进高风险复核
    calls: int = 0
    usage: dict = field(default_factory=dict)

    def passed(self) -> bool:
        return not self.hard_fails

    def profile(self) -> dict:
        return {k: v.get("answer") for k, v in self.items.items()}


DETAIL_RANK = {"无": 0, "模糊": 1, "具体": 2}


def comment_state(post: dict, text: str, *, account: str = "", reply_to_text: str = "", role: str = "") -> dict:
    st = {"说明": "以下是一篇小红书帖子和它下面的一条候选评论。只根据帖子和这条评论判断。",
          "帖子标题": post.get("title") or "（无标题）", "帖子正文": (post.get("body") or "")[:600]}
    if post.get("cover"):
        st["帖子封面"] = post["cover"]
    if account:
        st["评论账号"] = account
    if reply_to_text:
        st["回复对象"] = reply_to_text
    if role:
        st["评论角色"] = role
    st["评论原文"] = text
    return st


def judge_comment(client: JevClient, post: dict, text: str, slot: Optional[Slot], reader: Bank, *,
                  ops: Optional[Bank] = None, fill: Optional[dict] = None, reply_to_text: str = "",
                  subject_id: str = "cand") -> CommentJudgement:
    """判一条候选评论。slot 为 None 时只判、只打旗，不算硬伤（给 judge_comments / 回填用）。"""
    fill = fill or fill_for(post)
    left = unfilled(reader, fill)
    if left:
        raise ValueError(f"题库 {reader.name} 还有没填的占位符：{left}")
    cj = CommentJudgement(text=text)
    st = comment_state(post, text, reply_to_text=reply_to_text, role=(slot.role if slot else ""))
    r = judge_state(client, reader, subject_id, st, subject_type="comment", fill=fill)
    cj.items.update(r.items); cj.calls += r.calls; cj.usage = dict(r.usage or {})
    if ops is not None:
        r2 = judge_state(client, ops, subject_id, st, subject_type="comment", fill=fill)
        cj.items.update(r2.items); cj.calls += r2.calls; cj.usage = add_usage(cj.usage, r2.usage)
    a = cj.profile()
    missing = sorted(q for q, it in cj.items.items() if it.get("answer") is None)
    if missing:
        # 漏答 / 选项外：是非题和选择题一样处理 —— 不当硬伤（不会下发「现在判『None』」的修改单），整条进复核
        cj.flags.append("漏答"); cj.needs_review = True
    if a.get("speech_act") in ENDORSE:
        cj.flags.append("背书体"); cj.needs_review = True
    if a.get("register") == "文案腔":
        cj.flags.append("文案腔")
    if a.get("reader_value") == "只有评价":
        cj.flags.append("只有评价")
    if a.get("arranged") == "是":
        cj.flags.append("像安排的")
    if a.get("is_scripted") == "是":
        cj.flags.append("像脚本")
    cj.ambiguous = [q for q, it in cj.items.items() if it.get("ambiguous")]
    if slot is not None:
        p = lambda q: cj.items.get(q, {}).get("p")  # noqa: E731
        known = lambda q: a.get(q) is not None       # noqa: E731 — 没判出来的题不参与硬伤
        if known("speech_act") and a.get("speech_act") not in slot.speech_act:
            cj.hard_fails.append(("speech_act", a.get("speech_act"), "/".join(slot.speech_act), p("speech_act")))
        if a.get("names_brand") == "是" and not slot.may_name_brand:
            cj.hard_fails.append(("names_brand", "是", "否", p("names_brand")))
        if slot.must_echo and a.get("echoes_post") == "否":
            cj.hard_fails.append(("echoes_post", "否", "是", p("echoes_post")))
        if a.get("register") == "文案腔":
            cj.hard_fails.append(("register", "文案腔", "随手口语/认真分享", p("register")))
        if known("reader_value") and a.get("reader_value") not in slot.value_ok:
            cj.hard_fails.append(("reader_value", a.get("reader_value"), "/".join(slot.value_ok), p("reader_value")))
        if known("detail_level") and DETAIL_RANK.get(a.get("detail_level"), 0) < DETAIL_RANK.get(slot.min_detail, 0):
            cj.hard_fails.append(("detail_level", a.get("detail_level"), f"至少{slot.min_detail}", p("detail_level")))
        if ops is not None and slot.intent and a.get("comment_intent") not in (slot.intent, None):
            cj.hard_fails.append(("comment_intent", a.get("comment_intent"), slot.intent, p("comment_intent")))
    return cj


def score_comment(cj: CommentJudgement) -> float:
    """越小越好：硬伤每条 100；「像安排的」「像脚本」各 10；歧义每处 1。"""
    return 100.0 * len(cj.hard_fails) + 10.0 * (("像安排的" in cj.flags) + ("像脚本" in cj.flags)) + 1.0 * len(cj.ambiguous)


def comment_repair_plan(cj: CommentJudgement, banks: dict, fill: Optional[dict] = None) -> list:
    """修改单：只说这条评论现在判成什么、要改成什么、那个选项的定义是什么；不写改法。题干里的占位符按 fill 填好再下发。"""
    plan = []
    byq = {}
    for b in banks.values():
        merged = dict(b.fill_defaults); merged.update(fill or {})
        for q in b.questions:
            byq[q.id] = (q, merged)
    for qid, now, want, p in cj.hard_fails:
        if qid not in byq:
            continue
        q, merged = byq[qid]
        labels = [w.removeprefix("至少") for w in want.split("/")]   # 按前缀剥，不按字符集（lstrip 会误删以「至」「少」开头的 label）
        if q.jtype == "choice":
            defs = [fill_text(q.criteria[lab], merged) for lab in labels if lab in q.criteria]
        else:
            defs = [fill_text(q.criteria["true" if labels[0] == "是" else "false"], merged)]
        defn = " ／ ".join(d for d in defs if d)
        ptxt = f"（{p}）" if p is not None else ""
        plan.append({"qid": qid, "now": now, "want": want, "p": p, "definition": defn,
                     "instruction": f"「{fill_text(q.ask, merged)}」现在判「{now}」{ptxt}，要改成「{want}」" + (f"：{defn}" if defn else "")})
    if "像安排的" in cj.flags and not cj.hard_fails:
        plan.append({"qid": "arranged", "now": "是", "want": "否", "p": cj.items.get("arranged", {}).get("p"), "definition": "",
                     "instruction": "七题都过了但总判仍「像安排的」：多半是句子太完整、太顺；缩短、留一个口语痕迹。"})
    return plan


def comment_repair_prompt(post: dict, text: str, plan: list, slot: Optional[Slot] = None) -> str:
    lines = ["下面是一篇小红书帖子、它下面的一条候选评论和一张修改单。只按修改单改这条评论，长度和口吻保持原样，不要加评价词，不要加感叹。",
             "只输出改后的评论原文，不要解释。", "", f"帖子标题：{post.get('title', '')}", f"帖子正文：{(post.get('body') or '')[:600]}", "",
             f"候选评论：{text}", ""]
    if slot is not None:
        lines.append(f"这一位的要求：{slot.speech_act[0]}；{'可以' if slot.may_name_brand else '不要'}点名；{'要接住帖子里具体的事' if slot.must_echo else '不必接帖子'}。")
    lines.append("修改单：")
    for i, p in enumerate(plan, 1):
        lines.append(f"{i}. {p['instruction']}")
    return "\n".join(lines)


def repair_comment(generator: Generator, post: dict, text: str, plan: list, slot: Optional[Slot] = None) -> str:
    if not plan:
        return text
    out = generator(comment_repair_prompt(post, text, plan, slot), n=1)[0]
    return (out or "").strip().strip("「」\"'")


# ── 写中：best-of-k ──────────────────────────────────────────────────────

def best_of_k_comment(client: JevClient, generator: Generator, prompt: str, k: int, post: dict, slot: Slot, *,
                      reader: Bank, ops: Optional[Bank] = None, fill: Optional[dict] = None, reply_to_text: str = "") -> tuple:
    """返回 ((最好的一条文本, 它的判定), [(候选, 分数) 按分数升序], 本轮调用次数)。生成端一条都没给 → ValueError。"""
    cands = [c.strip() for c in generator(prompt, n=k) if c and c.strip()]
    if not cands:
        raise ValueError(f"生成端没有给出候选评论（位 {slot.id}）")
    judged = [(c, judge_comment(client, post, c, slot, reader, ops=ops, fill=fill, reply_to_text=reply_to_text, subject_id=f"{slot.id}:{i}"))
              for i, c in enumerate(cands)]
    judged.sort(key=lambda cj: score_comment(cj[1]))
    return judged[0], [(c, score_comment(j)) for c, j in judged], sum(j.calls for _, j in judged)


# ── 成组：评论区 ─────────────────────────────────────────────────────────

def thread_state(post: dict, comments: list) -> dict:
    """comments = [{"text", "account"?, "role"?, "reply_to_text"?}]"""
    lines = []
    for i, c in enumerate(comments, 1):
        who = c.get("account") or (c.get("role") or "读者")
        rep = f"（回复「{c['reply_to_text'][:20]}」）" if c.get("reply_to_text") else ""
        lines.append(f"{i}. [{who}]{rep} {c['text']}")
    return {"说明": "以下是一篇小红书帖子和它下面能看到的全部评论。只根据这些文字判断评论区整体。",
            "帖子标题": post.get("title") or "（无标题）", "帖子正文": (post.get("body") or "")[:600], "评论列表": "\n".join(lines)}


@dataclass
class ThreadJudgement:
    items: dict = field(default_factory=dict)
    hard_fails: list = field(default_factory=list)     # [(qid, now, want, p)]
    code: dict = field(default_factory=dict)           # 代码算的：点名条数、背书条数、条数
    unjudged: list = field(default_factory=list)       # [(qid, invalid_reason)]：评论区题没判出有效答案（漏答 / 选项外）
    calls: int = 0

    @property
    def needs_review(self) -> bool:
        return bool(self.unjudged)

    def passed(self) -> bool:
        # 漏答不是「没问题」：四题全漏也不能当评论区过了（codex review on #2）
        return not self.hard_fails and not self.unjudged


def judge_thread(client: JevClient, post: dict, comments: list, thread: Bank, *, fill: Optional[dict] = None,
                 judged: Optional[list] = None, max_named: int = 1, subject_id: str = "set") -> ThreadJudgement:
    """comments 同 thread_state；judged（可选）= 每条的 CommentJudgement，用来算代码特征。"""
    fill = fill or fill_for(post)
    tj = ThreadJudgement()
    r = judge_state(client, thread, subject_id, thread_state(post, comments), subject_type="comment", fill=fill)
    tj.items = r.items; tj.calls = r.calls
    tj.unjudged = [(q, (r.items.get(q) or {}).get("invalid_reason") or "missing")
                   for q in thread.ids() if (r.items.get(q) or {}).get("answer") is None]
    a = {k: v.get("answer") for k, v in r.items.items()}
    p = lambda q: r.items.get(q, {}).get("p")  # noqa: E731
    if a.get("praise_share") == "多数":
        tj.hard_fails.append(("praise_share", "多数", "少数/一半左右", p("praise_share")))
    if a.get("same_template") == "是":
        tj.hard_fails.append(("same_template", "是", "否", p("same_template")))
    if a.get("has_friction") == "否":
        tj.hard_fails.append(("has_friction", "否", "是", p("has_friction")))
    if a.get("thread_arranged") == "是":
        tj.hard_fails.append(("thread_arranged", "是", "否", p("thread_arranged")))
    # praise_share = 一半左右 放行（题库 on_ambiguous 就写「按一半左右处理」，是有意的中间档）；= 评论太少（v0.4 的出口）也放行，按歧义走
    if judged:
        named = sum(1 for cj in judged if cj.profile().get("names_brand") == "是")
        endorse = sum(1 for cj in judged if cj.profile().get("speech_act") in ENDORSE)
        tj.code = {"n": len(judged), "named": named, "endorse": endorse}
        if named > max_named:
            tj.hard_fails.append(("named_count", str(named), f"≤{max_named}", None))
    return tj


def thread_repair_plan(tj: ThreadJudgement, slots: list, judged: list) -> list:
    """评论区不过 → 指出哪一位要重出。每条 = {"slot": 位 id 或 None, "why": …}；slot 为 None = 换谁都修不了，问题在评论位配置，只记录不换。
    规则：
      · 同一句式 / 夸的太多 → 换掉背书体和文案腔的位；brief 硬放的背书位（slot 本身就要背书）不换 —— 重出还是同一类，标 needs_review。
      · 点名太多 → 换掉不该点名却点了名的位；点名的位全是允许点名的 → 配置问题（max_named 与 may_name_brand 打架）。
      · 没有摩擦 → 有贴主回复位就重出它（要真的回应读者）；没有就重出一个提问位（改成追问价格或效果）；两者都没有 → 配置问题。
      · 整体像安排的但单条都能过 → 换分数最差的一位。"""
    plan = []
    fails = {f[0] for f in tj.hard_fails}
    by_slot = {s.id: (s, cj) for s, cj in zip(slots, judged)}
    if fails & {"same_template", "praise_share"}:
        for sid, (s, cj) in by_slot.items():
            if "背书体" in cj.flags and set(s.speech_act) & set(ENDORSE):
                plan.append({"slot": None, "why": f"位 {sid} 是 brief 要求的背书位，重出还是背书体；评论区判同一句式 / 夸的太多时它会一直拖后腿，整组进复核"})
            elif "背书体" in cj.flags or "文案腔" in cj.flags:
                plan.append({"slot": sid, "why": "评论区被判同一句式或夸的太多，这一位是背书体 / 文案腔"})
    if "named_count" in fails:
        offenders = [sid for sid, (s, cj) in by_slot.items() if cj.profile().get("names_brand") == "是" and not s.may_name_brand]
        for sid in offenders:
            plan.append({"slot": sid, "why": "点名的评论超过上限，这一位不该点名"})
        if not offenders:
            plan.append({"slot": None, "why": "点名条数超过 max_named，但点了名的都是允许点名的位：是评论位配置和 max_named 打架，换谁都修不了"})
    if "has_friction" in fails:
        reply = next((sid for sid, (s, _) in by_slot.items() if s.role == "贴主" or s.reply_to), None)
        ask = next((sid for sid, (s, _) in by_slot.items() if "提问" in s.speech_act), None)
        if reply:
            plan.append({"slot": reply, "why": "评论区没有摩擦：贴主回复这一位要真的回应读者（答疑、承认没做到的地方），不能只是客套"})
        elif ask:
            plan.append({"slot": ask, "why": "评论区没有摩擦：这一位改成审慎的追问（问价格、问效果、问适不适合自己），别是中性的「在哪买」"})
        else:
            plan.append({"slot": None, "why": "评论区没有摩擦，评论位里既没有贴主回复位也没有提问位，换谁都补不出来：改 brief 的 comment_slots"})
    if tj.hard_fails and not plan:
        worst = max(by_slot.items(), key=lambda kv: score_comment(kv[1][1]))
        plan.append({"slot": worst[0], "why": f"评论区不过（{'、'.join(sorted(fails))}）但没有一位能单独归因：换掉分数最差的一位"})
    seen = set(); out = []
    for p in plan:
        key = p["slot"] if p["slot"] is not None else ("cfg", p["why"])
        if key not in seen:
            seen.add(key); out.append(p)
    return out


# ── 整条 ─────────────────────────────────────────────────────────────────

def produce_comments(client: JevClient, generator: Generator, post: dict, slots: list, prompt_for: Callable[[dict, Slot], str], *,
                     reader: Bank, thread: Bank, ops: Optional[Bank] = None, k: int = 3, max_repairs: int = 1,
                     max_thread_rounds: int = 1, max_named: int = 1) -> dict:
    """返回 {"comments": [{slot, text, profile, flags, passed, needs_review, hard_fails}], "thread": {...}, "trail": [...], "calls": n}"""
    fill = fill_for(post)
    banks = {b.name: b for b in (reader, ops) if b is not None}
    chosen: dict = {}      # slot id → (text, cj)
    trail = []; calls = 0

    def reply_text(slot: Slot) -> str:
        if slot.reply_to and slot.reply_to in chosen:
            return chosen[slot.reply_to][0]
        return ""

    def fill_slot(slot: Slot, why: str = "") -> None:
        nonlocal calls
        rt = reply_text(slot)
        # 换位的原因写进这一位的 note 再交给 prompt_for：生成端拿到的 prompt 和上一轮不同，才可能出不同的候选
        ask_slot = replace(slot, note=(slot.note + "；" if slot.note else "") + f"上一版被换掉的原因：{why}") if why else slot
        (text, cj), ranking, n_calls = best_of_k_comment(client, generator, prompt_for(post, ask_slot), k, post, slot,
                                                         reader=reader, ops=ops, fill=fill, reply_to_text=rt)
        calls += n_calls
        step = {"step": f"best_of_k:{slot.id}", "ranking": [s for _, s in ranking], "hard_fails": len(cj.hard_fails)}
        if why:
            step["why"] = why
        trail.append(step)
        rounds = 0
        while cj.hard_fails and rounds < max_repairs:
            plan = comment_repair_plan(cj, banks, fill)
            text = repair_comment(generator, post, text, plan, slot)
            cj = judge_comment(client, post, text, slot, reader, ops=ops, fill=fill, reply_to_text=rt, subject_id=f"{slot.id}:r{rounds + 1}")
            calls += cj.calls; rounds += 1
            trail.append({"step": f"repair_{rounds}:{slot.id}", "plan": plan, "hard_fails": len(cj.hard_fails)})
        chosen[slot.id] = (text, cj)

    ordered = sorted(slots, key=lambda s: 1 if s.reply_to else 0)   # 先出被回复的位，再出回复位
    for s in ordered:
        fill_slot(s)
    tj = None
    for rnd in range(max_thread_rounds + 1):
        comments = [{"text": chosen[s.id][0], "role": s.role, "reply_to_text": reply_text(s)} for s in slots]
        judged = [chosen[s.id][1] for s in slots]
        tj = judge_thread(client, post, comments, thread, fill=fill, judged=judged, max_named=max_named)
        calls += tj.calls
        trail.append({"step": f"thread_{rnd}", "items": {q: it.get("answer") for q, it in tj.items.items()}, "code": tj.code,
                      "hard_fails": [f[0] for f in tj.hard_fails]})
        if tj.passed() or rnd == max_thread_rounds:
            break
        tplan = thread_repair_plan(tj, slots, judged)
        trail[-1]["thread_plan"] = tplan
        refill: dict = {}                            # 位 id → 原因；被换的位若有人回复，回复位也重出；同一位只重出一次
        for p in tplan:
            if p["slot"] is None:
                continue                             # 配置问题：换谁都修不了，只记在轨迹里
            refill.setdefault(p["slot"], p["why"])
            for dep in slots:
                if dep.reply_to == p["slot"]:
                    refill.setdefault(dep.id, f"回复的对象 {p['slot']} 换了")
        if not refill:
            break
        for s in sorted(slots, key=lambda s: 1 if s.reply_to else 0):
            if s.id in refill:
                fill_slot(s, why=refill[s.id])
    out_comments = []
    for s in slots:
        text, cj = chosen[s.id]
        out_comments.append({"slot": s.id, "role": s.role, "reply_to": s.reply_to, "text": text, "profile": cj.profile(),
                             "flags": cj.flags, "passed": cj.passed(), "needs_review": cj.needs_review,
                             "hard_fails": cj.hard_fails, "ambiguous": cj.ambiguous})
    return {"comments": out_comments, "thread": {"items": {q: {"answer": it.get("answer"), "p": it.get("p")} for q, it in tj.items.items()},
                                                 "code": tj.code, "passed": tj.passed(), "hard_fails": tj.hard_fails,
                                                 "unjudged": tj.unjudged, "needs_review": tj.needs_review},
            "passed": tj.passed() and all(c["passed"] and not c["needs_review"] for c in out_comments),   # 要复核的不算过
            "needs_review": tj.needs_review or any(c["needs_review"] for c in out_comments), "trail": trail, "calls": calls}
