# -*- coding: utf-8 -*-
"""暗题的季度按钉死的时区算, 不按服务器本地时间 (TV 审计 2026-10-08 C-08)。

Railway 是 UTC、写手机器上的 MCP 是北京时间: 每季交界有 8 小时两边的 date.today() 不同季, 暗题集合不一样,
一边判了、另一边 redact 的题号对不上。钉 Asia/Shanghai, 两边一致; 要换用 JUDGE_TZ。"""
from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_quarter_default_is_shanghai_not_server_local(monkeypatch):
    monkeypatch.delenv("JUDGE_TZ", raising=False)
    import judge.hidden as H
    importlib.reload(H)
    assert H.QUARTER_TZ.key == "Asia/Shanghai"
    assert H.quarter() == H.quarter(H.today()), "不传日期 = 按钉死时区的今天"
    # 显式传日期的行为不变
    assert H.quarter(date(2026, 1, 1)) == "2026Q1" and H.quarter(date(2026, 12, 31)) == "2026Q4"


def test_quarter_boundary_differs_between_utc_and_shanghai():
    """交界那 8 小时: UTC 还是 Q3 的最后一天 16:00+, 上海已经是 Q4 —— 这正是以前两边不一致的窗口。"""
    import judge.hidden as H
    utc_moment = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)      # UTC 09-30 18:00
    assert H.quarter(utc_moment.date()) == "2026Q3"
    assert H.quarter(utc_moment.astimezone(H.QUARTER_TZ).date()) == "2026Q4", "上海已是 10-01 02:00"


def test_judge_tz_env_overrides(monkeypatch):
    monkeypatch.setenv("JUDGE_TZ", "UTC")
    import judge.hidden as H
    importlib.reload(H)
    try:
        assert H.QUARTER_TZ.key == "UTC"
        assert abs((H.today() - datetime.now(timezone.utc).date()).days) <= 1
    finally:
        monkeypatch.delenv("JUDGE_TZ", raising=False)
        importlib.reload(H)
        assert H.QUARTER_TZ.key == "Asia/Shanghai"
