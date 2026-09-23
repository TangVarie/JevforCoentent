# -*- coding: utf-8 -*-
"""切片：直接用 TV vendor 进来的 feature_bank.build_spans，保证和 TV 自己的抽取看到同样的片段。

判断题只看题目 scope 指定的那段（docs/28 §5.1）。state 里放四段：标题、正文第一句、正文最后一段、正文；
「标题和正文」（full）不再重复放一遍，题干里写「只看【标题】和【正文】」即可，省一半 token。
Mode A：这里绝不加入项目名、品牌、tier、互动数（D-017 / D-028）。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Optional

_VENDOR = Path(__file__).resolve().parent.parent / "banks" / "vendor" / "tv_feature_bank.py"
_WS = re.compile(r"\s+")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
MIN_BODY_CHARS = 20


def _load_tv_module():
    spec = importlib.util.spec_from_file_location("tv_feature_bank", _VENDOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_tv = _load_tv_module()


def build_spans(raw_content: str, mode: str = "markers", title_col: Optional[str] = None) -> dict:
    """mode = markers / column / none（mapping 的 title_extraction）。返回 TV 同款 spans dict。"""
    return _tv.build_spans(raw_content or "", mode=mode, title_col=title_col)


def bank_digest(raw: bytes) -> str:
    """题库校验和：用 TV 的规范化算法（剔掉顶格的 status: / frozen_sha256: 两行再 hash），
    与 TV 落进 note_feature_answers.bank_sha256 的值同口径；冻结题库那天 digest 不变，账本不会被劈成两批。"""
    return _tv.bank_digest(raw)


def state_from_spans(spans: dict) -> dict:
    st = {"说明": "下面是一篇小红书笔记，代码已切成几段。每道题只看题目指定的那一段，只根据文字判断。"}
    st["标题"] = spans["title"] if spans.get("title") else "（这篇没有标题）"
    st["正文第一句"] = spans.get("first_sentence") or "（切不出来）"
    st["正文最后一段"] = spans.get("last_para") or "（切不出来）"
    st["正文"] = spans.get("body") or ""
    return st


def askable(bank, spans: dict) -> tuple:
    """按 TV 的答题契约决定哪些题不问：no_title / text_too_short。返回 (要问的 id 列表, {跳过 id: 原因})。"""
    qids, skipped = [], {}
    body_len = len(_WS.sub("", spans.get("body") or ""))
    for q in bank.questions:
        sc = q.scope
        if sc is None:
            qids.append(q.id); continue
        if sc == "title" and not spans.get("title"):
            skipped[q.id] = "no_title"; continue
        if sc in ("body", "full") and body_len < MIN_BODY_CHARS:
            skipped[q.id] = "text_too_short"; continue
        if sc in ("first_sentence", "last_para") and len(_WS.sub("", spans.get(sc) or "")) < 2:
            skipped[q.id] = "text_too_short"; continue
        qids.append(q.id)
    return qids, skipped


def scope_text(spans: dict, scope: Optional[str]) -> str:
    if scope == "title":
        return spans.get("title") or ""
    if scope == "full":
        return spans.get("full") or ""
    return spans.get(scope or "body") or ""


def split_sentences(text: str, cap: int = 120) -> list:
    """按 。！？!?；; 和换行切句，太短的（<6 字）并入前一句。选句证据用。"""
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p and p.strip()]
    out = []
    for p in parts:
        if out and len(_WS.sub("", p)) < 6:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out[:cap]


def norm(s: str) -> str:
    return _WS.sub("", s or "").replace("“", '"').replace("”", '"')
