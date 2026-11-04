# -*- coding: utf-8 -*-
"""external-corpus.yml 的形态守卫（2026-10-08，TV 审计 A-08 / 运维 9；2026-11-04 加写库密钥前置检查，审计 11-02 F16）。

钉的是【被禁止的形态】：
  · 缺写库密钥时先花钱 —— 以前照样抓取、判定，写库那步退 0、job 绿、cache 存下 seen state，下周这批被跳过；
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
    hits = [s for s in steps if "apply_rows.py" in str(s.get("run", "")) and "--check-env" not in str(s.get("run", ""))]
    assert len(hits) == 1, "写库步（apply_rows.py rows.json）应该恰有一个"
    return hits[0]


def test_db_secrets_checked_before_any_paid_call():
    """缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 时在 known_ids、抓取（TikHub + Jev）之前就红（AU-F16）。"""
    steps = _doc()["jobs"]["crawl"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    check = [i for i, r in enumerate(runs) if "apply_rows.py --check-env" in r]
    assert len(check) == 1, "要有一步 apply_rows.py --check-env"
    first_paid = min(i for i, r in enumerate(runs) if "external_corpus.py" in r and "--dry-run" not in r)
    known = next(i for i, r in enumerate(runs) if "known_external_ids.py" in r)
    assert check[0] < known < first_paid, "密钥检查要排在 known_ids 和抓取之前"
    step = steps[check[0]]
    assert not step.get("continue-on-error"), "密钥检查不能 continue-on-error：缺密钥就该红"
    cond = str(step.get("if", ""))
    assert "always()" not in cond and "dry_run" in cond, "只在 dry_run 时跳过（dry_run 不联网、不写库）"
    env = _doc()["jobs"]["crawl"]["env"]
    assert {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"} <= set(env), "密钥要在 job 级 env 里，检查那一步才看得到"


def test_db_write_failure_must_make_the_job_red():
    step = _write_step()
    assert not step.get("continue-on-error"), (
        "写库步不能 continue-on-error：job 绿 + cache 存下 seen state + 账本没这批 = 那一周静默丢（A-08）")
    assert "always()" in str(step.get("if", "")), "产物上传之后仍要尝试写库（run 步 systemic 红时也写已有的行）"


def test_schedule_is_off_the_hour_and_twice_weekly():
    d = _doc()
    crons = [x["cron"] for x in d[True]["schedule"]] if True in d else [x["cron"] for x in d["on"]["schedule"]]
    assert len(crons) == 1
    minute, hour, dom, mon, dow = crons[0].split()
    assert minute != "0", "定时跑别放整点（GitHub 整点队列漂得最厉害）"
    assert dow == "1,4" and dom == "*" and mon == "*", "每周一、周四各一次（2026-11-03 起；改了要同步 config 的每月上限和每月最坏花费）"
    assert hour == "3", "小时不动：docs/29 登记的是 03:07 UTC，改了要同步登记册"


def test_known_ids_step_uses_the_ledger_aware_script():
    """known_ids 那一步必须跑 known_external_ids.py (它只收账本写齐的); 换成直接读 external_notes 就会把
    "external_notes 写成、账本写炸"的那批永久跳过 (codex review on #6, P1)。"""
    steps = _doc()["jobs"]["crawl"]["steps"]
    hits = [s for s in steps if "known_external_ids.py" in str(s.get("run", ""))]
    assert len(hits) == 1 and "state/known_ids.txt" in hits[0]["run"]
    src = (ROOT / "scripts" / "known_external_ids.py").read_text(encoding="utf-8")
    assert "note_feature_answers" in src and "complete_ids" in src, "known_ids 的判据必须看账本, 不能只看 external_notes"


def test_single_flight_concurrency_group():
    d = _doc()
    conc = d.get("concurrency")
    assert isinstance(conc, dict) and conc.get("group") == "external-corpus", "要有 concurrency 组：手动触发撞上定时跑不能并行"
    assert conc.get("cancel-in-progress") is False, "排队等，不打断在飞的那次（它已经花了钱）"
