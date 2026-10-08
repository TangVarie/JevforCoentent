# -*- coding: utf-8 -*-
"""external-corpus.yml 的三条形态守卫（2026-10-08，TV 审计 A-08 / 运维 9）。

钉的是【被禁止的形态】：
  · 写库步带 continue-on-error —— run 步在写库前已 save_state，写库失败而 job 绿 = actions/cache 把 seen-but-unwritten
    的 state 存下来，账本没这批、known_ids 也没有，下周按 seen 跳过：那一周静默丢；
  · 定时跑在整点 —— GitHub 整点队列漂得最厉害；
  · 没有 concurrency 组 —— 手动触发撞上定时跑，两个 run 各恢复同一份 state、各花一遍钱、后存的 cache 盖掉先存的。
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "external-corpus.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _write_step() -> dict:
    steps = _doc()["jobs"]["crawl"]["steps"]
    hits = [s for s in steps if "apply_rows.py" in str(s.get("run", ""))]
    assert len(hits) == 1, "写库步（apply_rows.py）应该恰有一个"
    return hits[0]


def test_db_write_failure_must_make_the_job_red():
    step = _write_step()
    assert not step.get("continue-on-error"), (
        "写库步不能 continue-on-error：job 绿 + cache 存下 seen state + 账本没这批 = 那一周静默丢（A-08）")
    assert "always()" in str(step.get("if", "")), "产物上传之后仍要尝试写库（run 步 systemic 红时也写已有的行）"


def test_schedule_is_off_the_hour_and_weekly():
    d = _doc()
    crons = [x["cron"] for x in d[True]["schedule"]] if True in d else [x["cron"] for x in d["on"]["schedule"]]
    assert len(crons) == 1
    minute, hour, dom, mon, dow = crons[0].split()
    assert minute != "0", "定时跑别放整点（GitHub 整点队列漂得最厉害）"
    assert dow == "1" and dom == "*" and mon == "*", "每周一一次"
    assert hour == "3", "小时不动：docs/29 登记的是 03:07 UTC，改了要同步登记册"


def test_single_flight_concurrency_group():
    d = _doc()
    conc = d.get("concurrency")
    assert isinstance(conc, dict) and conc.get("group") == "external-corpus", "要有 concurrency 组：手动触发撞上定时跑不能并行"
    assert conc.get("cancel-in-progress") is False, "排队等，不打断在飞的那次（它已经花了钱）"
