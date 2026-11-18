# -*- coding: utf-8 -*-
"""cover-pilot.yml 的形态守卫：只能手动点、默认不花钱、密钥只进那一步且只在 run 模式、产物只有两个计数报告、
和 external-corpus 不共用并发组；抓取时段的拒跑窗口从 external-corpus.yml 的 cron 推出来核对（那边改了时间这里就红）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts import cover_pilot_core as K

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "cover-pilot.yml"
CORPUS = ROOT / ".github" / "workflows" / "external-corpus.yml"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _on(d: dict) -> dict:
    return d.get("on", d.get(True))                    # YAML 1.1 把裸 on 解析成 True


def _steps() -> list:
    return _doc()["jobs"]["pilot"]["steps"]


def _step(name: str) -> dict:
    return next(s for s in _steps() if s.get("name") == name)


def test_dispatch_only_with_safe_defaults():
    on = _on(_doc())
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["type"] == "choice" and inputs["mode"]["options"] == ["plan", "rehearse", "run"]
    assert inputs["mode"]["default"] == "plan"
    assert inputs["recheck_minutes"]["type"] == "choice"
    assert {int(x) for x in inputs["recheck_minutes"]["options"]} <= set(K.RECHECK_CHOICES)
    assert inputs["confirm"].get("default", "") == ""


def test_guard_needs_confirm_and_main():
    g = _step("Guard")
    assert g["if"] == "inputs.mode == 'run' && (inputs.confirm != 'I-35' || github.ref != 'refs/heads/main')"
    assert "exit 1" in g["run"]
    assert _steps()[0] is not None and _steps()[0].get("name") == "Guard"


def test_secrets_only_on_the_pilot_step_and_paid_ones_only_in_run_mode():
    d = _doc()
    assert "env" not in d and "env" not in d["jobs"]["pilot"]
    for s in _steps():
        if s.get("name") != "Pilot":
            assert "env" not in s, s.get("name") or s.get("uses")
            assert "secrets." not in str(s) and "github.token" not in str(s)
    env = _step("Pilot")["env"]
    assert env["TIKHUB_API_KEY"] == "${{ inputs.mode == 'run' && secrets.TIKHUB_API_KEY || '' }}"
    assert env["GITHUB_TOKEN"] == "${{ inputs.mode == 'run' && github.token || '' }}"
    assert env["SUPABASE_URL"] == "${{ inputs.mode != 'rehearse' && secrets.SUPABASE_URL || '' }}"
    assert env["SUPABASE_SERVICE_ROLE_KEY"] == "${{ inputs.mode != 'rehearse' && secrets.SUPABASE_SERVICE_ROLE_KEY || '' }}"
    secret_names = {x.split("secrets.")[1].split()[0] for x in env.values() if "secrets." in x}
    assert secret_names == {"TIKHUB_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"}     # 没有新密钥
    assert "env" not in _step("Install")


def test_no_input_is_interpolated_into_a_shell_line():
    for s in _steps():
        run = str(s.get("run", ""))
        assert "${{" not in run, s.get("name")
    assert "inputs.confirm" not in str(_step("Pilot"))


def test_exit_codes_zero_and_three_are_green():
    run = _step("Pilot")["run"]
    assert "set +e" in run and 'if [ "$rc" != 0 ] && [ "$rc" != 3 ]; then exit "$rc"; fi' in run
    assert "scripts/cover_pilot.py" in run and '--summary "$GITHUB_STEP_SUMMARY"' in run


def test_permissions_concurrency_checkout_timeout():
    d = _doc()
    assert d["permissions"] == {"contents": "write"}
    assert d["concurrency"]["group"] == "cover-pilot" and d["concurrency"]["cancel-in-progress"] is False
    co = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert co["with"]["persist-credentials"] is False
    assert d["jobs"]["pilot"]["timeout-minutes"] <= 60
    assert K.RECHECK_BUDGET_MIN + 10 <= d["jobs"]["pilot"]["timeout-minutes"]


def test_artifact_is_the_two_counts_only_reports_with_short_retention():
    up = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert 1 <= up["with"]["retention-days"] <= 7
    assert up["with"]["path"].split() == ["out/cover_pilot_report.json", "out/cover_pilot_report.md"]
    assert "always()" in up["if"]


def _cron_starts():
    """external-corpus.yml 的每个定时起跑时刻（UTC）：(周几, 时, 分)。周几按 cron（0 = 周日）→ Python（0 = 周一）。"""
    doc = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    out = []
    for entry in _on(doc)["schedule"]:
        minute, hour, dom, month, dow = entry["cron"].split()
        assert dom == "*" and month == "*"
        for d in dow.split(","):
            out.append(((int(d) - 1) % 7, int(hour), int(minute)))
    return out


def test_crawl_window_covers_every_scheduled_external_corpus_run():
    """external-corpus 和试点共用一个 TikHub 账户：它的每次定时起跑（加上 GitHub 定时最常见的几十分钟延迟和它 40 分钟的上限）
    都要落在拒跑窗口里。那边改了 cron，这里就红，逼着一起改 CRAWL_DAYS / CRAWL_HOURS。"""
    starts = _cron_starts()
    assert starts
    corpus_timeout = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["jobs"]["crawl"]["timeout-minutes"]
    base = datetime(2026, 11, 16, tzinfo=timezone.utc)                 # 周一
    for wd, h, m in starts:
        start = base + timedelta(days=wd, hours=h, minutes=m)
        assert K.in_crawl_window(start)
        assert K.in_crawl_window(start - timedelta(minutes=60))          # 试点自己最多跑 60 分钟：起跑前一小时也拒
        assert K.in_crawl_window(start + timedelta(hours=6, minutes=corpus_timeout))   # 定时延迟几个小时也还在窗口里
    assert {wd for wd, _, _ in starts} == set(K.CRAWL_DAYS)


def test_external_corpus_workflow_is_untouched_by_the_pilot():
    doc = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    assert doc["concurrency"]["group"] == "external-corpus"
    assert "cover_pilot" not in CORPUS.read_text(encoding="utf-8")
