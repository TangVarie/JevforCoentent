# -*- coding: utf-8 -*-
"""数据出境配置、暗题配置缺失或坏了要直接报错，不再静默回落（2026-11-04，审计 11-02 F15；/health 的 policy_sha256 顺手关 J-10）。

以前：
  · policy.load() 缺文件回 {} —— 处方药名单整个变空，处方药闸等于没装（今天唯一的处方药项目碰巧已放行才没出事）；
  · hidden._config() 缺文件回 {} —— 没有暗题，暗题的题号和答案原样回给写手；
  · YAML 写坏（比如 * 没带引号）在请求时才炸成 500，部署照样绿。
现在：读不出、少键多键、名单形状不对都抛；服务启动两份都先校验（坏部署 /health 不绿）；运行中 /health 重新校验、哪份坏了都 503，
别的端点读到坏的那份才 503、在调 Jev 之前返回（不读那份的端点照常）。
合格配置的行为一点不变。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import hidden as H  # noqa: E402
from judge import policy as P  # noqa: E402

GOOD = "rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: [\"*\"]\n"


def _policy_file(tmp_path, monkeypatch, text):
    p = tmp_path / "data_policy.yaml"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(P, "CONFIG", p)
    return p


def _hidden_file(tmp_path, monkeypatch, text):
    p = tmp_path / "hidden_rotation.yaml"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(H, "CONFIG", p)
    return p


def _rewrite(p, text):
    """同一路径重写暗题配置。缓存按 (路径, 修改时间) 认，粗粒度时钟的文件系统上连写几次 mtime 可能不变，
    所以每次都从上一次的 mtime 往后拨 5 秒，保证缓存一定换新。"""
    old = p.stat().st_mtime
    p.write_text(text, encoding="utf-8")
    os.utime(p, (p.stat().st_atime, old + 5))


# ── policy.load() ──

def test_repo_policy_config_is_valid_and_sha_is_file_bytes():
    cfg = P.load()
    assert set(cfg) == set(P.REQUIRED_KEYS)
    assert P.config_sha256() == hashlib.sha256((ROOT / "config" / "data_policy.yaml").read_bytes()).hexdigest()


def test_missing_policy_file_raises_instead_of_empty(tmp_path, monkeypatch):
    _policy_file(tmp_path, monkeypatch, None)
    with pytest.raises(P.PolicyConfigError, match="不存在"):
        P.load()
    # 以前这里回 {}：处方药项目的未发布稿会被当成普通项目放过去
    with pytest.raises(P.PolicyConfigError):
        P.decide(["aw_version"], "OKMAN")
    with pytest.raises(P.PolicyConfigError):
        P.project_layer_cleared("TUGE")
    with pytest.raises(P.PolicyConfigError):
        P.config_sha256()


@pytest.mark.parametrize("text, why", [
    ("rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: [*]\n", "读不出"),     # * 没带引号
    ("", "顶层要是映射"),                                                                              # 空文件
    ("- a\n- b\n", "顶层要是映射"),
    ("rx_categories: [处方药]\nrx_projects: [OKMAN]\nproject_layer_cleared: [TUGE]\n", "缺 ['rx_cleared']"),
    (GOOD + "rx_project: [NEWRX]\n", "多出 ['rx_project']"),                                           # 拼错的键不能被悄悄忽略
    ("rx_categories: [处方药]\nrx_projects: OKMAN\nrx_cleared: []\nproject_layer_cleared: [TUGE]\n", "rx_projects 要是字符串列表"),
    ("rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared:\nproject_layer_cleared: [TUGE]\n", "rx_cleared 要是字符串列表"),
    ("rx_categories: [处方药]\nrx_projects: [OKMAN, 3]\nrx_cleared: []\nproject_layer_cleared: [TUGE]\n", "rx_projects 要是字符串列表"),
    ("rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: [\"*\"]\nproject_layer_cleared: [TUGE]\n", "不能写 \"*\""),
])
def test_broken_policy_config_raises(tmp_path, monkeypatch, text, why):
    _policy_file(tmp_path, monkeypatch, text)
    with pytest.raises(P.PolicyConfigError) as exc:
        P.load()
    assert why in str(exc.value)


def test_valid_policy_behaviour_unchanged(tmp_path, monkeypatch):
    _policy_file(tmp_path, monkeypatch, "rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: [TUGE]\n")
    with pytest.raises(P.PolicyBlocked):
        P.decide(["aw_version"], "OKMAN")
    assert P.decide(["aw_version"], "TUGE").project_layer is True
    assert P.decide(["aw_version"], "NRT").project_layer is False
    assert P.decide(["note"], "OKMAN").published is True
    with pytest.raises(P.PolicyInputError):
        P.decide(["aw_version"], None)


# ── hidden._config() ──

def test_repo_hidden_config_is_valid():
    cfg = H.check_config()
    assert "human_feel_para" in cfg["banks"]


def test_missing_hidden_file_raises_instead_of_no_hidden(tmp_path, monkeypatch):
    _hidden_file(tmp_path, monkeypatch, None)
    with pytest.raises(H.HiddenConfigError, match="不存在"):
        H.hidden_ids("human_feel_para_v0.2", ["a", "b", "c"])        # 以前回 set()：暗题原样回给写手


@pytest.mark.parametrize("text, why", [
    ("banks:\n  human_feel_para:\n    fraction: [0.3\n", "读不出"),
    ("", "顶层要是只有 banks"),
    ("banks:\n", "顶层要是只有 banks"),
    ("bank:\n  human_feel_para: {fraction: 0.33}\n", "顶层要是只有 banks"),
    ("banks:\n  human_feel_para: {fraction: 0}\n", "fraction"),
    ("banks:\n  human_feel_para: {fraction: 1.5}\n", "fraction"),
    ("banks:\n  human_feel_para: {fraction: \"1/3\"}\n", "fraction"),
    ("banks:\n  human_feel_para: {fractoin: 0.33}\n", "只能写 fraction / override"),
    ("banks:\n  human_feel_para: {override: {2026-Q4: [para_register]}}\n", "override 写错了"),
    ("banks:\n  human_feel_para: {override: {2026Q4: para_register}}\n", "override 写错了"),
])
def test_broken_hidden_config_raises(tmp_path, monkeypatch, text, why):
    _hidden_file(tmp_path, monkeypatch, text)
    with pytest.raises(H.HiddenConfigError) as exc:
        H.hidden_ids("human_feel_para_v0.2", ["a", "b", "c"])
    assert why in str(exc.value)


def test_valid_hidden_configs_behaviour_unchanged(tmp_path, monkeypatch):
    qids = ["q1", "q2", "q3", "q4", "q5", "q6"]
    p = _hidden_file(tmp_path, monkeypatch, "banks:\n  human_feel_para:\n    fraction: 0.3333\n    override: {}\n")
    auto = H.hidden_ids("human_feel_para_v0.2", qids)
    assert len(auto) == 2 and H.hidden_ids("comment_reader_v0.6", qids) == set()
    _rewrite(p, "banks:\n  human_feel_para:\n")                                                # 空条目 = 默认 1/3
    assert H.hidden_ids("human_feel_para_v0.2", qids) == auto
    _rewrite(p, "banks:\n  human_feel_para:\n    override: {2026Q4: [q5, zz]}\n")
    from datetime import date
    assert H.hidden_ids("human_feel_para_v0.2", qids, when=date(2026, 11, 4)) == {"q5"}


def test_broken_hidden_config_is_not_cached(tmp_path, monkeypatch):
    p = _hidden_file(tmp_path, monkeypatch, "banks: [\n")
    with pytest.raises(H.HiddenConfigError):
        H.check_config()
    _rewrite(p, "banks:\n  human_feel_para: {fraction: 0.5}\n")
    assert H.check_config()["banks"]["human_feel_para"]["fraction"] == 0.5


# ── HTTP：启动校验、/health、运行中坏了 503 ──

def test_health_echoes_policy_sha256(monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1")
    from fastapi.testclient import TestClient
    from judge.api import app
    with TestClient(app) as c:                                  # 带 with 才跑启动校验
        h = c.get("/health")
    assert h.status_code == 200 and h.json()["ok"] is True
    assert h.json()["policy_sha256"] == hashlib.sha256((ROOT / "config" / "data_policy.yaml").read_bytes()).hexdigest()
    assert "config_error" not in h.json()


def test_startup_refuses_broken_policy(tmp_path, monkeypatch):
    _policy_file(tmp_path, monkeypatch, "rx_categories: [处方药]\n")
    from fastapi.testclient import TestClient
    from judge.api import app
    with pytest.raises(P.PolicyConfigError):
        with TestClient(app):
            pass


def test_startup_refuses_missing_hidden_config(tmp_path, monkeypatch):
    _hidden_file(tmp_path, monkeypatch, None)
    from fastapi.testclient import TestClient
    from judge.api import app
    with pytest.raises(H.HiddenConfigError):
        with TestClient(app):
            pass


@pytest.mark.parametrize("which", ["policy", "hidden"])
def test_health_503_when_config_breaks_at_runtime(tmp_path, monkeypatch, which):
    monkeypatch.setenv("JUDGE_MOCK", "1")
    from fastapi.testclient import TestClient
    from judge.api import app
    c = TestClient(app)
    (_policy_file if which == "policy" else _hidden_file)(tmp_path, monkeypatch, None)
    h = c.get("/health")
    assert h.status_code == 503
    d = h.json()
    assert d["ok"] is False and d["policy_sha256"] is None and "不存在" in d["config_error"]
    assert "auth" in d and "banks" in d                         # 其余字段照回，方便一眼看全


def test_endpoints_503_without_any_jev_call_when_policy_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge import api as A

    def no_jev():
        raise AssertionError("配置坏了不该走到调 Jev 这一步")
    monkeypatch.setattr(A, "_client", no_jev)
    c = TestClient(A.app)
    _policy_file(tmp_path, monkeypatch, None)
    H_ = {"X-Judge-Key": "k"}
    body = "昨天在药店买了一盒东西，嚼了几口辣嗓子，有点想戒了。大家怎么看？还有什么办法吗，求推荐。"
    r = c.post("/judge_draft", json={"title": "t", "body": body, "project": "OKMAN"}, headers=H_)
    assert r.status_code == 503 and r.json()["detail"].startswith("config:")
    r = c.post("/judge", json={"bank": "feature_questions_v0_1", "project": "OKMAN",
                               "subjects": [{"subject_type": "aw_version", "subject_id": "d1", "raw_content": body}]}, headers=H_)
    assert r.status_code == 503
    r = c.post("/compile_rules", json={"project": "OKMAN", "rules": [{"id": "r1", "text": "标题别写最便宜", "severity": "hard"}]}, headers=H_)
    assert r.status_code == 503
