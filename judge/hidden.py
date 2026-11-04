# -*- coding: utf-8 -*-
"""暗题（docs/00 #4、docs/31 §2.2 / §8）：人感题库的 1/3 不写进任何 prompt，每季度换。

暗题照判（段级）、照落账本、照算段级分布（给写手看的那份去掉暗题的字段）。暗题不算硬伤、不影响 passed：人感题库只在段级判，
硬伤只看篇级的 hard_rules（fq / 平台 / 项目层）；暗题命中时修改单里只说「有一项不公开的检查没过」（2026-11-04 改正，审计 11-02 F03：
以前这里写「照算硬伤」，和代码不符）。它的题号、题干、定义、答案不出现在任何会被写手或生成端看到的输出里：
修改单（repair_plan）、MCP 的 list_banks / judge_draft / repair_plan_for、HTTP /judge_draft 返回的 profile / detail /
hard_fails / ambiguous / para_stats / plan。账本行（ledger_rows）照常带全部题，那是写库用的数据，调用方不得转给写手。

选哪几道：config/hidden_rotation.yaml 里按题库配 fraction（默认 1/3）；每个季度按 sha256(题号 + 季度) 排序取前 round(n × fraction) 道（至少 1 道），
季度一换自动轮换，不用人记得去改。要手工指定某个季度，在 override 里写死。换季（自动或手工）记一笔在题库维护记录里。
配置文件缺失、读不出或形状不对 → 直接抛 HiddenConfigError（2026-11-04，审计 11-02 F15）：以前缺文件时静默当「没有暗题」，
暗题的题号和答案就会原样回给写手。服务启动时先校验一遍，坏部署 /health 就失败。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
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


class HiddenConfigError(RuntimeError):
    """config/hidden_rotation.yaml 缺失、读不出或形状不对。不回落成「没有暗题」（审计 11-02 F15）。"""


_QUARTER = re.compile(r"^\d{4}Q[1-4]$")
_BANK_KEYS = {"fraction", "override"}


def validate(cfg) -> dict:
    """形状校验：顶层只有 banks（映射）；每个题库前缀下可写 fraction（0 到 1 之间，不含 0）和 override（「2026Q4: [题号, …]」）。
    写错的季度名（如 2026-Q4）以前会被悄悄忽略、那一季照旧按 sha256 自动选，现在直接报错。"""
    if not isinstance(cfg, dict) or set(cfg) != {"banks"} or not isinstance(cfg.get("banks"), dict):
        got = sorted(map(str, cfg)) if isinstance(cfg, dict) else type(cfg).__name__
        raise HiddenConfigError(f"暗题配置 {CONFIG} 顶层要是只有 banks 一个键的映射（banks 下按题库前缀写），读到的是 {got}")
    for prefix, c in cfg["banks"].items():
        if c is None:
            continue
        if not isinstance(c, dict) or not set(c) <= _BANK_KEYS:
            raise HiddenConfigError(f"暗题配置 {CONFIG} 的 {prefix} 只能写 fraction / override，读到的是 {c!r}")
        f = c.get("fraction", 1 / 3)
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not 0 < f <= 1:
            raise HiddenConfigError(f"暗题配置 {CONFIG} 的 {prefix}.fraction 要是 0 到 1 之间的数（不含 0），读到的是 {f!r}")
        override = c.get("override")
        if override is None:
            continue
        if not isinstance(override, dict):
            raise HiddenConfigError(f"暗题配置 {CONFIG} 的 {prefix}.override 要是映射（「2026Q4: [题号, …]」），读到的是 {override!r}")
        for q, ids in override.items():
            if not _QUARTER.match(str(q)) or not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids):
                raise HiddenConfigError(f"暗题配置 {CONFIG} 的 {prefix}.override 写错了：{q!r}: {ids!r}（季度写成 2026Q4，值是题号列表）")
    return cfg


def _config() -> dict:
    """读并校验暗题配置，按 (路径, 修改时间) 缓存。缺文件、YAML 坏、形状不对都抛 HiddenConfigError，坏的不进缓存。"""
    try:
        key = (str(CONFIG), CONFIG.stat().st_mtime)
        if key not in _cache:
            cfg = validate(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))
            _cache.clear()
            _cache[key] = cfg
    except FileNotFoundError:
        raise HiddenConfigError(f"暗题配置不存在：{CONFIG}（部署要带上 config/hidden_rotation.yaml，或用 JUDGE_HIDDEN_CONFIG 指对位置）") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HiddenConfigError(f"暗题配置 {CONFIG} 读不出：{exc}") from None
    return _cache[key]


def check_config() -> dict:
    """服务启动和 /health 用：校验一遍暗题配置，坏了抛 HiddenConfigError。"""
    return _config()


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
