# -*- coding: utf-8 -*-
"""题库：两种文件格式，一种内部表示。

  · Jev 格式（本仓 banks/*.yaml）：questions[].type = choice / noul，instructions.zh，options[].label/zh，
    criteria."true"/"false"，unclear_labels，feeds，on_ambiguous。评论题库就是这种。
  · TV 格式（banks/vendor/feature_questions_v0_1.yaml，原样 vendor，记校验和）：questions[].type = bool / choice，
    ask / yes_if / no_if / yes_examples / no_examples / options[].value/means/example / scope / evidence。
    代码特征（code:v1）和占位题不在这里处理，那是 TV 自己的事。

内部表示（Bank / Question）只关心：id、Jev 题型、题干、选项定义、看哪一段（scope）、要不要证据、歧义规则。
纪律：题干与定义一改 → bank_version 升一号 → 重跑金标准。bank_sha256 落库时随行记录（同 D-041）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

SCOPE_LABEL = {"title": "标题", "first_sentence": "正文第一句", "last_para": "正文最后一段",
               "body": "正文", "full": "标题和正文"}
DEFAULT_AMBIGUITY = {"choice_top_min": 0.60, "choice_margin_min": 0.20, "noul_band": [0.35, 0.65]}
LANG_DEFAULT = "zh"


@dataclass
class Question:
    id: str
    jtype: str                      # "choice" | "noul"
    instructions: str
    criteria: dict                  # choice: {label: 定义}; noul: {"true": 定义, "false": 定义}
    scope: Optional[str] = None     # TV 格式才有：title / first_sentence / last_para / body / full
    evidence: str = "never"         # never | on_true | on_choice_except:<值,...>
    unclear_labels: list = field(default_factory=list)
    feeds: str = ""
    on_ambiguous: str = ""
    ask: str = ""                   # 原始题干（证据选句时引用）
    version: int = 1
    aw_instruction: str = ""        # TV 格式才有：过闸后下发给写作台的正向写法（never = 永不下发）

    def needs_evidence(self, answer) -> bool:
        if self.evidence == "never" or answer is None:
            return False
        if self.evidence == "on_true":
            return answer in (True, "是")
        if self.evidence.startswith("on_choice_except:"):
            exc = [x for x in self.evidence.split(":", 1)[1].split(",") if x]
            return answer not in exc
        return False


@dataclass
class Bank:
    name: str
    version: str
    model: str
    fmt: str                        # "jev" | "tv"
    questions: list
    ambiguity: dict
    path: Optional[Path] = None
    sha256: str = ""
    fill_defaults: dict = field(default_factory=dict)   # 题干里 {占位符} 的默认值（评论题库按篇填要点、按项目填主体）

    def by_id(self) -> dict:
        return {q.id: q for q in self.questions}

    def ids(self) -> list:
        return [q.id for q in self.questions]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fmt_examples(xs) -> str:
    xs = [str(x) for x in (xs or []) if x]
    return ("例如：" + "／".join(xs)) if xs else ""


def _load_tv(raw: dict, path: Path, name: str) -> Bank:
    qs = []
    for q in raw.get("questions", []):
        if q.get("retired"):
            continue
        sc = q["scope"]
        look = "只看【标题】和【正文】这两段。" if sc == "full" else f"只看【{SCOPE_LABEL[sc]}】这一段。"
        if q["type"] == "bool":
            crit = {"true": ((q.get("yes_if") or "") + " " + _fmt_examples(q.get("yes_examples"))).strip(),
                    "false": ((q.get("no_if") or "") + " " + _fmt_examples(q.get("no_examples"))).strip()}
            ev = "never" if str(q.get("evidence", "")).startswith("不需要") else "on_true"
            qs.append(Question(q["id"], "noul", look + q["ask"], crit, scope=sc, evidence=ev,
                               ask=q["ask"], version=int(q.get("version", 1)), aw_instruction=str(q.get("aw_instruction") or "")))
        else:
            crit = {o["value"]: ((o.get("means") or "") + (f" 例如：{o['example']}" if o.get("example") else "")).strip()
                    for o in q["options"]}
            if str(q.get("evidence", "")).startswith("不需要"):
                ev = "never"
            elif q["id"] == "product_role":
                ev = "on_choice_except:未出现"
            else:
                ev = "never"
            qs.append(Question(q["id"], "choice", look + q["ask"], crit, scope=sc, evidence=ev,
                               ask=q["ask"], version=int(q.get("version", 1)), aw_instruction=str(q.get("aw_instruction") or "")))
    return Bank(name=name, version=raw.get("bank_version", "?"), model=raw.get("model") or "jev-1.13.0",
                fmt="tv", questions=qs, ambiguity=raw.get("ambiguity") or dict(DEFAULT_AMBIGUITY), path=path,
                sha256=file_sha256(path))


def _load_jev(raw: dict, path: Path, name: str, lang: str = LANG_DEFAULT) -> Bank:
    qs = []
    for q in raw.get("questions", []):
        text = q["instructions"].get(lang) or q["instructions"]["zh"]
        if q["type"] == "choice":
            crit = {o["label"]: (o.get(lang) or o["zh"]) for o in q["options"]}
            qs.append(Question(q["id"], "choice", text, crit, unclear_labels=list(q.get("unclear_labels") or []),
                               feeds=q.get("feeds", ""), on_ambiguous=q.get("on_ambiguous", ""), ask=text))
        else:
            c = q["criteria"]
            crit = {"true": c["true"].get(lang) or c["true"]["zh"], "false": c["false"].get(lang) or c["false"]["zh"]}
            qs.append(Question(q["id"], "noul", text, crit, feeds=q.get("feeds", ""), evidence=str(q.get("evidence") or "never"),
                               on_ambiguous=q.get("on_ambiguous", ""), ask=text))
    return Bank(name=name, version=raw.get("bank_version", "?"), model=raw.get("model") or "jev-1.13.0",
                fmt="jev", questions=qs, ambiguity=raw.get("ambiguity") or dict(DEFAULT_AMBIGUITY), path=path,
                sha256=file_sha256(path), fill_defaults={k: str(v) for k, v in (raw.get("fill_defaults") or {}).items()})


def load_bank(path: str | Path, name: Optional[str] = None, lang: str = LANG_DEFAULT) -> Bank:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    name = name or path.stem
    first = (raw.get("questions") or [{}])[0]
    if "ask" in first or first.get("type") == "bool":
        return _load_tv(raw, path, name)
    return _load_jev(raw, path, name, lang)


def check_bank(bank: Bank) -> list:
    problems = []
    ids = bank.ids()
    if len(ids) != len(set(ids)):
        problems.append("题目 id 有重复")
    for key in ("choice_top_min", "choice_margin_min", "noul_band"):
        if key not in bank.ambiguity:
            problems.append(f"ambiguity 缺 {key}")
    for q in bank.questions:
        if not q.instructions:
            problems.append(f"{q.id}: 缺题干")
        if q.jtype == "choice" and len(q.criteria) < 2:
            problems.append(f"{q.id}: 选项少于两个")
        if q.jtype == "noul" and not (q.criteria.get("true") and q.criteria.get("false")):
            problems.append(f"{q.id}: 是非题缺 true/false 定义")
        for k, v in q.criteria.items():
            if not str(v).strip():
                problems.append(f"{q.id}: 选项「{k}」定义为空")
    return problems


def fill_text(text: str, fill: Optional[dict]) -> str:
    """把题干 / 定义里的 {占位符} 换成本篇的值。只做字面替换，不用 str.format（定义里可能有别的花括号）。"""
    if not fill or "{" not in text:
        return text
    for k, v in fill.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def unfilled(bank: Bank, fill: Optional[dict] = None) -> list:
    """填完之后仍留着 {占位符} 的题（说明调用方漏给了）。"""
    merged = dict(bank.fill_defaults); merged.update(fill or {})
    left = []
    for q in bank.questions:
        texts = [q.instructions] + [str(v) for v in q.criteria.values()]
        if any("{" in fill_text(t, merged) and "}" in fill_text(t, merged) for t in texts):
            left.append(q.id)
    return left


def build_questions(bank: Bank, qids: Optional[list] = None, fill: Optional[dict] = None) -> dict:
    merged = dict(bank.fill_defaults); merged.update(fill or {})
    out = {}
    for q in bank.questions:
        if qids is not None and q.id not in qids:
            continue
        out[q.id] = {"type": q.jtype, "instructions": fill_text(q.instructions, merged),
                     "criteria": {k: fill_text(str(v), merged) for k, v in q.criteria.items()}}
    return out


def interpret(bank: Bank, resp: dict) -> dict:
    """Jev 原始答案 → {qid: {answer, p(所选答案概率), p_yes(是非题), top3, ambiguous, why}}"""
    amb = bank.ambiguity
    lo, hi = amb["noul_band"]
    raw = resp.get("answers") or {}
    out = {}
    for q in bank.questions:
        a = raw.get(q.id)
        if not a:
            continue
        if a.get("type") == "noul" or (q.jtype == "noul" and "noul" in a):
            p = float(a.get("noul", 0.5))
            yes = p > 0.5
            reasons = [f"概率 {p:.2f} 落在 {lo}–{hi}"] if lo <= p <= hi else []
            out[q.id] = {"answer": "是" if yes else "否", "raw": yes, "p": round(max(p, 1 - p), 3),
                         "p_yes": round(p, 3), "ambiguous": bool(reasons), "why": "；".join(reasons)}
        else:
            probs = sorted(((k, float(v)) for k, v in (a.get("probabilities") or {}).items()), key=lambda kv: -kv[1])
            p1 = probs[0][1] if probs else 0.0
            p2 = probs[1][1] if len(probs) > 1 else 0.0
            reasons = []
            if p1 < amb["choice_top_min"]:
                reasons.append(f"第一名 {p1:.2f} < {amb['choice_top_min']}")
            if p1 - p2 < amb["choice_margin_min"]:
                reasons.append(f"前两名只差 {p1 - p2:.2f}")
            if a.get("choice") in q.unclear_labels:
                reasons.append(f"选了「{a.get('choice')}」")
            out[q.id] = {"answer": a.get("choice"), "raw": a.get("choice"), "p": round(p1, 3),
                         "confidence": a.get("confidence"), "top3": [[k, round(v, 3)] for k, v in probs[:3]],
                         "ambiguous": bool(reasons), "why": "；".join(reasons)}
    return out


def discover(dir_: str | Path) -> dict:
    """banks/ 目录下所有题库：{name: path}。vendor 里的 TV 题库也算，名字用文件名。"""
    dir_ = Path(dir_)
    found = {}
    for p in sorted(dir_.glob("*.yaml")) + sorted((dir_ / "vendor").glob("*.yaml")):
        found[p.stem] = p
    return found
