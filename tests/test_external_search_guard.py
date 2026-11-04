# -*- coding: utf-8 -*-
"""外部语料：搜索接口 HTTP 200 但业务报错 / 返回格式变了，不能每页白付费还一直绿着（外部评审 2026-11-04 F09）。

以前：顶层 code 500、外壳 200 里包着 data.success=false、字段改成驼峰（noteCard / noteId）这三种，_find_notes 都只回 []，
run_once 当成「这页搜索成功、没结果」，整次 0 候选、systemic_failure 为空、job 绿；带着 search_id 的还会接着付费翻第 2 页。
现在：业务报错抛 SearchUnparseable（当这页搜索失败），整次搜了若干页却 0 候选是系统性故障，某品类搜了 ≥2 页 0 候选单独告警，
第一份认不出笔记的原始返回脱敏后进产物 search_samples。单个关键词真没结果照旧绿。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import external as E  # noqa: E402
from judge.jev_client import JevClient  # noqa: E402

FQ = ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml"
SECRET = "SECRET-VALUE-123"


def _banks():
    return (B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1"),
            B.load_bank(FQ, name="feature_questions_v0_1"))


def _cfg(n_cat: int = 1) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))
    cfg["categories"] = cfg["categories"][:n_cat]
    assert int(cfg["pages_per_sort"]) >= 2                  # 生产配置每个关键词×排序翻 2 页：要验证「不再往后翻」
    return cfg


BUSINESS_500 = {"code": 500, "message": "upstream error", "params": {"token": SECRET}, "data": None}
NESTED_FAIL = {"code": 200, "message": "Request successful. This request will incur a charge.",
               "data": {"success": False, "code": 300011, "msg": "服务异常", "search_id": "sid-1"}}
CAMEL_DRIFT = {"code": 200, "data": {"code": 0, "success": True,
                                     "data": {"search_id": "sid-1", "items": [{"noteCard": {"noteId": "x1", "displayTitle": "t"}}] * 20}}}


def _provider(payload_for):
    """search 请求照样计费，返回 payload_for(params)；返回 None 时走 mock 供应商。详情走 mock。"""
    class P(E.TikHubClient):
        def _get(self, path, params):
            if path == E.SEARCH_PATH:
                got = payload_for(params)
                if got is not None:
                    self.budget.charge(1)
                    return json.loads(json.dumps(got))
            return super()._get(path, params)
    return P(budget=E.Budget(limit_usd=6.0), mock=True)


@pytest.mark.parametrize("raw,why", [(BUSINESS_500, "code=500"), (NESTED_FAIL, "data.success=false"),
                                     ({"code": 200, "data": {"code": -1, "msg": "x"}}, "data.code=-1"),
                                     ({"code": "429"}, "code=429"), ([], "返回不是 JSON 对象")])
def test_business_error_detected(raw, why):
    assert E._search_business_error(raw) == why


@pytest.mark.parametrize("raw", [E.mock_search({"keyword": "戒烟", "page": 1}), CAMEL_DRIFT,
                                 {"code": 200, "data": {"code": 0, "data": [{"note_list": []}]}},
                                 {"code": "200", "data": {"success": True, "code": 1000}}, {"data": {"items": []}}])
def test_healthy_shapes_are_not_business_errors(raw):
    assert E._search_business_error(raw) == ""


@pytest.mark.parametrize("payload", [BUSINESS_500, NESTED_FAIL])
def test_business_error_makes_every_search_fail_and_the_run_red(payload):
    triage, fq = _banks()
    cfg = _cfg()
    prov = _provider(lambda params: payload)
    rep = E.run_once(cfg, prov, JevClient(mock=True), triage, fq, {"seen": {}, "monthly": {}})
    n_pairs = len(cfg["categories"][0]["keywords"]) * len(cfg["sorts"])
    assert rep.searched == 0 and rep.search_attempts == n_pairs, "业务报错算这页失败，这个关键词×排序不再翻页"
    assert prov.budget.calls == n_pairs
    assert rep.systemic_failure and all(e.startswith("SearchUnparseable") for _, e in rep.errors)
    assert len(rep.search_samples) == E.SEARCH_SAMPLES
    dumped = json.dumps([rep.errors, rep.search_samples], ensure_ascii=False)
    assert SECRET not in dumped, "原始返回进报错 / 产物之前脱敏"
    assert rep.warnings and "搜索全部出错" in rep.warnings[0]


def test_shape_drift_with_search_id_is_red_and_stops_paging():
    triage, fq = _banks()
    cfg = _cfg()
    prov = _provider(lambda params: CAMEL_DRIFT)
    st = {"seen": {}, "monthly": {}}
    rep = E.run_once(cfg, prov, JevClient(mock=True), triage, fq, st)
    n_pairs = len(cfg["categories"][0]["keywords"]) * len(cfg["sorts"])
    assert rep.candidates == 0 and rep.searched == rep.search_attempts == n_pairs, "首页一条都没认出来就不付费翻第 2 页"
    assert prov.budget.calls == n_pairs and rep.errors == []
    assert "没认出一条笔记" in rep.systemic_failure and "系统性故障" in E.render_report(rep, cfg)
    assert len(rep.search_samples) == 1 and rep.search_samples[0]["raw"] == CAMEL_DRIFT and st["seen"] == {}


def test_one_empty_category_warns_but_stays_green():
    triage, fq = _banks()
    cfg = _cfg(2)
    dead = set(cfg["categories"][0]["keywords"])
    prov = _provider(lambda params: CAMEL_DRIFT if params["keyword"] in dead else None)
    rep = E.run_once(cfg, prov, JevClient(mock=True), triage, fq, {"seen": {}, "monthly": {}})
    assert rep.systemic_failure == "" and rep.judged > 0
    name = cfg["categories"][0]["name"]
    assert len(rep.warnings) == 1 and rep.warnings[0].startswith(name) and "一条笔记都没认出来" in rep.warnings[0]
    assert "注意：" in E.render_report(rep, cfg)


def test_one_empty_keyword_among_full_ones_stays_green_and_quiet():
    triage, fq = _banks()
    cfg = _cfg()
    empty_kw = cfg["categories"][0]["keywords"][0]
    calls = []

    def payload(params):
        if params["keyword"] == empty_kw:
            calls.append(params["page"])
            return {"code": 200, "data": {"code": 0, "success": True, "data": {"search_id": "sid-e", "items": []}}}
        return None

    rep = E.run_once(cfg, _provider(payload), JevClient(mock=True), triage, fq, {"seen": {}, "monthly": {}})
    assert rep.systemic_failure == "" and rep.warnings == [] and rep.errors == [] and rep.judged > 0
    assert calls == [1] * len(cfg["sorts"]), "真没结果的关键词：首页空就不翻第 2 页"
    assert len(rep.search_samples) == 1 and rep.search_samples[0]["keyword"] == empty_kw


def test_healthy_mock_run_keeps_no_search_samples():
    triage, fq = _banks()
    rep = E.run_once(_cfg(), E.TikHubClient(budget=E.Budget(limit_usd=6.0), mock=True), JevClient(mock=True), triage, fq,
                     {"seen": {}, "monthly": {}})
    assert rep.search_samples == [] and rep.warnings == [] and rep.systemic_failure == ""


def test_masking_replaces_secret_like_fields_and_the_api_key():
    raw = {"a": {"Authorization": "Bearer x", "cookie": "c", "items": ["k-123 in text"]}, "sign": "", "title": "ok"}
    assert E._masked(raw, "k-123") == {"a": {"Authorization": "***", "cookie": "***", "items": ["*** in text"]}, "sign": "", "title": "ok"}
