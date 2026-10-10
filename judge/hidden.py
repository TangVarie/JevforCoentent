# -*- coding: utf-8 -*-
"""暗题（docs/00 #4、docs/31 §2.2 / §8）：人感题库的 1/3 不写进任何 prompt，每季度换。

暗题照判、照落账本、照算硬伤和段级分布；只是它的题号、题干、定义、答案不出现在任何会被写手或生成端看到的输出里：
修改单（repair_plan）、MCP 的 list_banks / judge_draft / repair_plan_for、HTTP /judge_draft 返回的 profile / detail /
hard_fails / ambiguous / para_stats / plan。账本行（ledger_rows）照常带全部题，那是写库用的数据，调用方不得转给写手。

选哪几道：config/hidden_rotation.yaml 里按题库配 fraction（默认 1/3）；每个季度按 sha256(题号 + 季度) 排序取前 round(n × fraction) 道（至少 1 道），
季度一换自动轮换，不用人记得去改。要手工指定某个季度，在 override 里写死。换季（自动或手工）记一笔在题库维护记录里。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

CONFIG = Path(os.environ.get("JUDGE_HIDDEN_CONFIG", Path(__file__).resolve().parent.parent / "config" / "hidden_rotation.yaml"))
_cache: dict = {}
_log = logging.getLogger("judge.hidden")
# 季度按哪个时区的"今天"算 (TV 审计 2026-10-08 C-08): 以前是 date.today() = 服务器本地时间, Railway 是 UTC、
# 起 judge.mcp_server 的机器 (当时是写手机器) 是北京时间, 每季交界有 8 小时两边暗题不一样。钉死一个时区, 两边一致; 要换用 JUDGE_TZ。
QUARTER_TZ_NAME = os.environ.get("JUDGE_TZ", "Asia/Shanghai")
# 起 MCP 的机器可能是 Windows / 精简镜像, 没有系统 IANA 时区库: requirements.txt 声明了 tzdata 兜底, 这里再兜一层 ——
# 找不到就退到固定偏移 (上海 +8, 其它退 UTC) 并记一行; 不能让一个时区名把 API / MCP 服务起不来 (codex review on #6)。
_FIXED_OFFSETS = {"Asia/Shanghai": 8, "UTC": 0}


def _load_tz(name: str, zone_cls=ZoneInfo) -> tzinfo:
    try:
        return zone_cls(name)
    except (ZoneInfoNotFoundError, OSError, ValueError) as exc:
        hours = _FIXED_OFFSETS.get(name, 0)
        _log.warning("时区库里找不到 %s (%s): 退到固定偏移 UTC%+d (季度判定不受影响; 想要真时区 pip install tzdata)", name, exc, hours)
        return timezone(timedelta(hours=hours), name)


QUARTER_TZ = _load_tz(QUARTER_TZ_NAME)


def today() -> date:
    return datetime.now(QUARTER_TZ).date()


def quarter(d: Optional[date] = None) -> str:
    d = d or today()
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _config() -> dict:
    key = (str(CONFIG), CONFIG.stat().st_mtime if CONFIG.exists() else None)
    if key not in _cache:
        _cache.clear()
        _cache[key] = (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}) if CONFIG.exists() else {}
    return _cache[key]


def hidden_ids(bank_name: str, qids: list, when: Optional[date] = None) -> set:
    """这个题库这个季度的暗题。bank_name 按前缀匹配配置（human_feel_para_v0.2 命中 human_feel_para 那一条）。"""
    cfg = None
    for prefix, c in (_config().get("banks") or {}).items():
        if bank_name.startswith(prefix):
            cfg = c or {}
            break
    if cfg is None or not qids:
        return set()
    q = quarter(when)
    override = (cfg.get("override") or {}).get(q)
    if override:
        return set(override) & set(qids)
    n = max(1, math.floor(len(qids) * float(cfg.get("fraction", 1 / 3)) + 0.5))   # 6 题 × 1/3 = 2 道；四舍五入，至少 1 道
    ranked = sorted(qids, key=lambda x: hashlib.sha256(f"{x}|{q}".encode("utf-8")).hexdigest())
    return set(ranked[:n])


def hidden_map(banks: dict, when: Optional[date] = None) -> dict:
    """{bank_name: {暗题 id}}，只列有暗题的题库。"""
    out = {}
    for name, b in banks.items():
        h = hidden_ids(name, b.ids(), when)
        if h:
            out[name] = h
    return out


def all_hidden(banks: dict, when: Optional[date] = None) -> set:
    return set().union(*hidden_map(banks, when).values()) if banks else set()
