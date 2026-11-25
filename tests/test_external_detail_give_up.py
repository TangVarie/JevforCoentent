# -*- coding: utf-8 -*-
"""外部语料：同一篇取全文时返回里一直找不到正文（DetailUnparseable），连续几趟就不再取（truth-vault S-18）。

11-16、11-19、11-23 三趟里同样几篇每趟都取不到正文：不进 seen，每趟重新分诊、重新付费取全文，
同一趟换个关键词再碰到还会再取一次（11-23 那趟 4 篇花了 6 次）。现在：
  · 同一趟里取过一次、返回里没有正文的，再碰到不再分诊、不再取全文；
  · 计数放在 state 文件的 detail_fail 里（GitHub Actions cache 保存，不动库）；
  · 连续 DETAIL_GIVE_UP_RUNS 个算数的趟次都这样就记进 seen（why = detail_unparseable），以后不再取；
  · 算数的趟次 = 这一趟至少取到一篇全文、且没有因连续 DETAIL_PROBE 篇取全文失败整次停；不算数的趟不加，也不因这一篇这趟没取到就清；
  · 清零不看这一趟算不算数：这一篇取到一次全文（之后 Jev 没判成、没进 seen 也算；同一趟先取到、再碰到时找不到正文的按取到算、不加）
    或进了 seen，就清零；
  · 这一篇只碰到请求层报错（超时 / 5xx）的趟不加也不清。
全合成样本（假供应商 + 假 Jev），零付费调用。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import json

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import external as E  # noqa: E402
from judge.jev_client import JevClient, JevError  # noqa: E402

BAD = "badnote0001"          # 合成 id；每一页搜索结果的第一条都是它
MARK = "合成正文"            # mode = ok_marked 时 BAD 的全文以它开头，假 Jev 认得出是 BAD 的 fq 打标
VOICES = ("普通用户", "达人测评", "商家或品牌", "媒体或科普", "说不清")


def _banks():
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    return triage, B.load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1")


def _cfg(cap: int = 30) -> dict:
    """一个品类、每种排序一页；cap 要大过一页（21 条），第二页搜索才会再碰到 BAD。"""
    cfg = yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))
    cfg["categories"] = cfg["categories"][:1]; cfg["pages_per_sort"] = 1; cfg["max_keep_per_category_per_run"] = cap
    return cfg


class KeepAllJev:
    """假 Jev：分诊一律留（on_topic 是、voice 普通用户），别的题照 mock；记下 BAD 被分诊了几次。"""
    mock = True

    def __init__(self):
        self.inner = JevClient(mock=True)
        self.bad_triage = 0

    def call(self, body):
        r = self.inner.call(body)
        if "on_topic" in body["questions"]:
            if body["state"].get("标题", "").startswith("合成"):
                self.bad_triage += 1
            ans = dict(r["answers"])
            ans["on_topic"] = {"type": "noul", "noul": 0.95}
            ans["voice"] = {"type": "choice", "choice": "普通用户",
                            "probabilities": {v: (0.96 if v == "普通用户" else 0.01) for v in VOICES}}
            r = dict(r, answers=ans)
        return r


class FailBadFqJev(KeepAllJev):
    """分诊同 KeepAllJev；BAD 的全文（以 MARK 开头）取到之后 fq 打标没判成：
    incomplete = Jev 回了 200 却一题没答（→ JevIncomplete）；error = Jev 报错（JevError）。"""

    def __init__(self, how: str):
        super().__init__()
        self.how = how

    def call(self, body):
        if "on_topic" not in body["questions"] and MARK in json.dumps(body.get("state"), ensure_ascii=False):
            if self.how == "error":
                raise JevError("合成的 Jev 报错")
            return dict(self.inner.call(body), answers={})
        return super().call(body)


class Provider(E.TikHubClient):
    """假供应商：每页搜索结果最前面插一条 BAD；BAD 的详情按 mode 回：
    unparseable = 外壳 200、里面没有正文（同 11-23 那几篇的样子）；timeout = 请求层报错；ok = 正常正文；
    ok_marked = 正常正文、以 MARK 开头（FailBadFqJev 认得出）；
    ok_then_unparseable = BAD 的第一次详情同 ok_marked，同一个供应商（同一趟）之后再取都同 unparseable。
    all_bad = True 时每一篇的详情都没有正文（详情接口整个坏了）。listed = 搜到过的全部 note_id。"""

    def __init__(self, mode: str = "unparseable", all_bad: bool = False, ok_first: int = 0):
        super().__init__(budget=E.Budget(limit_usd=5.0), mock=True)
        self.mode, self.all_bad, self.ok_first = mode, all_bad, ok_first
        self.details = Counter()
        self.listed: set = set()

    def _get(self, path, params):
        raw = super()._get(path, params)
        if path == E.SEARCH_PATH:
            bad = {"note_id": BAD, "title": "合成标题", "desc": "合成摘要，讲自己的一段经历。", "user": {"nickname": "合成"},
                   "interact_info": {"liked_count": 1}, "type": "normal"}
            raw["data"]["items"] = [bad] + raw["data"]["items"]
            self.listed.update(i["note_id"] for i in raw["data"]["items"])
            return raw
        nid = params["note_id"]
        self.details[nid] += 1
        n = sum(self.details.values())
        if self.all_bad and n > self.ok_first:
            return {"code": 200, "message": "合成的上游失败"}
        if nid == BAD and (self.mode == "unparseable" or (self.mode == "ok_then_unparseable" and self.details[BAD] > 1)):
            return {"code": 200, "message": "合成的上游失败"}
        if nid == BAD and self.mode == "timeout":
            raise TimeoutError("合成超时")
        if nid == BAD and self.mode in ("ok_marked", "ok_then_unparseable"):
            raw["data"]["note"] = dict(raw["data"]["note"], title="合成全文标题", desc=MARK + "：" + raw["data"]["note"]["desc"])
        return raw


def _run(state, prov=None, jev=None, cap=30, known_ids=None):
    triage, fq = _banks()
    prov = prov or Provider()
    jev = jev or KeepAllJev()
    rep = E.run_once(_cfg(cap), prov, jev, triage, fq, state, known_ids=known_ids)
    return rep, prov, jev


def test_unparseable_note_is_fetched_once_per_run_and_counted_in_state():
    state = {"seen": {}, "monthly": {}}
    rep, prov, jev = _run(state)
    assert rep.searched >= 2, "要有第二页搜索才看得到「同一趟再碰到」"
    assert prov.details[BAD] == 1 and jev.bad_triage == 1, "同一趟换个关键词 / 排序再碰到：不再分诊、不再付费取全文"
    assert BAD not in state["seen"] and state["detail_fail"][BAD]["runs"] == 1
    assert rep.detail_unparseable == 1 and rep.detail_given_up == 0 and rep.systemic_failure == ""
    others = {k: v for k, v in prov.details.items() if k != BAD}
    assert others and set(others.values()) == {1} and rep.fetched == len(others), "好的笔记照旧一篇取一次"
    md = E.render_report(rep, _cfg())
    assert "取全文返回里找不到正文 1 篇" in md and f"连续 {E.DETAIL_GIVE_UP_RUNS} 趟" in md
    assert "出错的（不进 seen，下次会再看）：" in md and "已记进 seen" not in md


def test_gives_up_after_consecutive_runs_and_stops_paying():
    state = {"seen": {}, "monthly": {}}
    for i in range(1, E.DETAIL_GIVE_UP_RUNS):
        rep, prov, _ = _run(state)
        assert prov.details[BAD] == 1 and state["detail_fail"][BAD]["runs"] == i and BAD not in state["seen"]
    rep, prov, _ = _run(state)                               # 第 DETAIL_GIVE_UP_RUNS 趟：还取一次，取不到就记进 seen
    assert prov.details[BAD] == 1 and rep.detail_given_up == 1
    assert state["seen"][BAD]["why"] == "detail_unparseable" and state["seen"][BAD]["kept"] is False
    assert BAD not in state["detail_fail"] and rep.given_up_ids == [BAD]
    md = E.render_report(rep, _cfg())                        # 出错列表不能说它「不进 seen、下次会再看」：它这一趟进了 seen
    line = next(x for x in md.splitlines() if x.startswith(f"- {BAD}:"))
    assert line.endswith("（已记进 seen，以后不再取）") and "其余不进 seen" in md and "出错的（不进 seen，下次会再看）" not in md
    rep, prov, jev = _run(state)                             # 之后：不分诊、不取全文
    assert prov.details[BAD] == 0 and jev.bad_triage == 0 and rep.detail_unparseable == 0 and rep.duplicates >= 1


def test_a_successful_fetch_resets_the_count():
    state = {"seen": {}, "monthly": {}}
    for _ in range(E.DETAIL_GIVE_UP_RUNS - 1):
        _run(state)
    assert state["detail_fail"][BAD]["runs"] == E.DETAIL_GIVE_UP_RUNS - 1
    _run(state, Provider(mode="ok"))                         # 这一趟取到了：有了结局，计数清掉
    assert BAD in state["seen"] and state["seen"][BAD]["why"] != "detail_unparseable"
    assert state["detail_fail"] == {}


@pytest.mark.parametrize("how", ["incomplete", "error"])
def test_a_fetch_that_succeeds_resets_the_count_even_if_judging_then_fails(how):
    """这一趟 BAD 的全文取到了，是后面 Jev 没判成（JevIncomplete）或报错（JevError）：不进 seen，但正文取得到，连续断了，
    计数清掉；之后要再连续 DETAIL_GIVE_UP_RUNS 个算数的趟取不到才放弃。"""
    state = {"seen": {}, "monthly": {}}
    for _ in range(E.DETAIL_GIVE_UP_RUNS - 1):
        _run(state)
    assert state["detail_fail"][BAD]["runs"] == E.DETAIL_GIVE_UP_RUNS - 1
    rep, prov, _ = _run(state, Provider(mode="ok_marked"), FailBadFqJev(how))
    assert prov.details[BAD] >= 1 and BAD in {w for w, _ in rep.errors} and rep.detail_unparseable == 0
    assert BAD not in state["seen"] and BAD not in state["detail_fail"], "取到过全文就清零，不只是进了 seen 才清"
    for i in range(1, E.DETAIL_GIVE_UP_RUNS):
        _run(state)
        assert BAD not in state["seen"] and state["detail_fail"][BAD]["runs"] == i


def test_an_uncounted_run_still_clears_the_count_when_the_body_was_fetched():
    """这一趟因连续 DETAIL_PROBE 篇取全文失败整次停（不算数），但停之前 BAD 的全文取到过、之后 Jev 没判成：
    不算数只管「不加」，不管「不清」：BAD 已有的计数照样清零；已经进了 seen 的旧条目也照样清。"""
    state = {"seen": {"elsewhere1": {"why": "kept", "kept": True}}, "monthly": {},
             "detail_fail": {BAD: {"runs": E.DETAIL_GIVE_UP_RUNS - 1, "at": "合成"}, "elsewhere1": {"runs": 1, "at": "合成"}}}
    rep, prov, _ = _run(state, Provider(mode="ok_marked", all_bad=True, ok_first=2), FailBadFqJev("incomplete"))
    assert "连续" in rep.stopped_reason and rep.fetched > 0 and not rep.systemic_failure, "要的是一趟被 DETAIL_PROBE 停掉、不算数的趟"
    assert prov.details[BAD] == 1 and BAD in {w for w, _ in rep.errors} and rep.detail_unparseable == E.DETAIL_PROBE
    assert BAD not in state["seen"] and BAD not in state["detail_fail"], "不算数的趟里取到过全文：计数照样清零"
    assert state["detail_fail"] == {}, "进了 seen 的旧条目不算数的趟里也清；这趟找不到正文的几篇不加一"


def test_fetched_then_unparseable_in_the_same_run_is_not_counted():
    """同一趟里 BAD 先在第一页取到全文、Jev 没判成（不进 seen），换一页再碰到时返回里找不到正文：
    这一趟按「取到过」算，不加一；已有的计数清零（算数的趟里也一样），之后要从 1 重新数。"""
    for prev in (None, {"runs": E.DETAIL_GIVE_UP_RUNS - 1, "at": "合成"}):
        state = {"seen": {}, "monthly": {}, "detail_fail": {BAD: prev} if prev else {}}
        rep, prov, jev = _run(state, Provider(mode="ok_then_unparseable"), FailBadFqJev("incomplete"))
        assert prov.details[BAD] == 2 and jev.bad_triage == 2 and rep.detail_unparseable == 1, "要先取到、再在同一趟碰到时取不到"
        assert rep.fetched > 0 and rep.stopped_reason == "", "要的是一趟算数的趟"
        assert BAD not in state["seen"] and rep.detail_given_up == 0
        assert BAD not in state["detail_fail"], f"同一趟先取到过全文：不加一、已有的计数清零（prev={prev}）"
        _run(state)                                          # 下一趟只是取不到：从 1 重新数
        assert state["detail_fail"][BAD]["runs"] == 1 and BAD not in state["seen"]


def test_no_count_when_only_the_bad_note_was_looked_at():
    """这一趟只走到 BAD 一篇（别的都在 known_ids 里）：一篇全文都没取到（fetched == 0），只失败 1 篇、没到 DETAIL_PROBE 整次停。
    分不清是这一篇的事还是详情接口的事：不加一、不记进 seen，连跑 DETAIL_GIVE_UP_RUNS 趟也一样；已有的计数也原样留着。"""
    _, listing, _ = _run({"seen": {}, "monthly": {}}, cap=1000)       # 先把每一页搜到的 note_id 都记下来
    known = listing.listed - {BAD}
    assert known
    state = {"seen": {}, "monthly": {}}
    for _ in range(E.DETAIL_GIVE_UP_RUNS):
        rep, prov, jev = _run(state, known_ids=known)
        assert rep.processed == 1 and rep.fetched == 0 and rep.stopped_reason == ""
        assert prov.details == Counter({BAD: 1}) and jev.bad_triage == 1 and rep.detail_unparseable == 1
        assert rep.detail_given_up == 0 and BAD not in state["seen"] and state["detail_fail"] == {}
    state["detail_fail"] = {BAD: {"runs": E.DETAIL_GIVE_UP_RUNS - 1, "at": "合成"}}
    _run(state, known_ids=known)
    assert state["detail_fail"] == {BAD: {"runs": E.DETAIL_GIVE_UP_RUNS - 1, "at": "合成"}} and BAD not in state["seen"]


def test_request_errors_are_not_counted_and_still_retried_within_the_run():
    """超时 / 5xx 是请求层的错，不一定是这一篇的事：不计数、同一趟再碰到照旧再取（和以前一样）。"""
    state = {"seen": {}, "monthly": {}}
    rep, prov, _ = _run(state, Provider(mode="timeout"))
    assert prov.details[BAD] >= 2 and rep.detail_unparseable == 0
    assert state["detail_fail"] == {} and BAD not in state["seen"]


def test_no_count_when_the_detail_endpoint_itself_looks_down():
    """一篇全文都没取到（整个详情接口坏了）或连续失败整次停：分不清是哪一篇的事，这一趟不计数。"""
    state = {"seen": {}, "monthly": {}}
    rep, _, _ = _run(state, Provider(all_bad=True))
    assert rep.fetched == 0 and rep.systemic_failure and state["detail_fail"] == {} and state["seen"] == {}
    state = {"seen": {}, "monthly": {}}
    rep, _, _ = _run(state, Provider(all_bad=True, ok_first=3))   # 前几篇取到了，后面接口坏了、连续 5 篇失败整次停
    assert rep.fetched > 0 and "连续" in rep.stopped_reason and not rep.systemic_failure
    assert state["detail_fail"] == {}


def test_stale_or_malformed_detail_fail_entries_do_not_break_the_run():
    """state 是 cache 里的 JSON：detail_fail 形状不对时当空的；已经进了 seen 的条目清掉。"""
    state = {"seen": {"elsewhere1": {"why": "kept", "kept": True}}, "monthly": {}, "detail_fail": {"elsewhere1": {"runs": 2}, BAD: {"runs": "x"}}}
    rep, prov, _ = _run(state)
    assert prov.details[BAD] == 1 and state["detail_fail"] == {BAD: {"runs": 1, "at": rep.started}}
    state = {"seen": {}, "monthly": {}, "detail_fail": ["坏的"]}
    rep, _, _ = _run(state)
    assert state["detail_fail"][BAD]["runs"] == 1
