# -*- coding: utf-8 -*-
"""I-35 封面试点（scripts/cover_pilot.py + cover_pilot_core.py）：记账、状态词、停止规则、选样、比对、下载、结论规则。
全部离线：假 TV / TikHub / GitHub / 图片服务器（tests/cover_pilot_fakes.py），主机在 .invalid 下。"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import cover_pilot as P
from scripts import cover_pilot_core as K
from tests import cover_pilot_fakes as F


# ── 1. 记账：10 次、0.75 元、Jev 的 Budget 三道，任何输入都抬不高 ──

def test_ledger_allows_exactly_ten_calls():
    L = K.Ledger()
    for _ in range(10):
        L.charge_detail()
    with pytest.raises(K.CapReached):
        L.charge_detail()
    assert L.detail_calls == 10 and L.yuan == 0.72


def test_ledger_yuan_cap_binds_when_price_is_higher(monkeypatch):
    monkeypatch.setattr(K, "PRICE_YUAN", 0.08)
    L = K.Ledger()
    for _ in range(9):
        L.charge_detail()                      # 9 × 0.08 = 0.72
    with pytest.raises(K.CapReached, match="yuan"):
        L.charge_detail()                      # 0.80 > 0.75
    assert L.detail_calls == 9


def test_jev_budget_is_an_independent_second_cap(monkeypatch):
    monkeypatch.setattr(K, "MAX_CALLS", 100)
    monkeypatch.setattr(K, "MAX_YUAN", 100.0)
    L = K.Ledger()
    for _ in range(10):
        L.charge_detail()
    with pytest.raises(K.CapReached, match="budget"):
        L.charge_detail()
    assert L.detail_calls == 10


def test_caps_cannot_be_raised_from_the_command_line_or_workflow():
    src = (P.ROOT / "scripts" / "cover_pilot.py").read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', src))
    assert flags == {"--out", "--summary", "--recheck-minutes"}
    assert "MAX_CALLS =" not in src and "MAX_YUAN =" not in src
    assert K.MAX_CALLS == 10 and K.MAX_YUAN == 0.75 and K.PRICE_YUAN == round(K.USD_PER_CALL * K.FX, 4)
    assert K.MAX_CALLS * K.PRICE_YUAN <= K.MAX_YUAN


def test_image_and_balance_counters_have_their_own_caps():
    L = K.Ledger()
    L.charge_balance_read(); L.charge_balance_read()
    with pytest.raises(K.CapReached):
        L.charge_balance_read()
    for _ in range(K.MAX_IMAGE_REQUESTS):
        L.charge_image()
    with pytest.raises(K.CapReached):
        L.charge_image()
    assert L.detail_calls == 0


# ── 2. 先记账再发，发出去失败照样算钱，不重试 ──

def test_every_send_is_charged_first_and_never_retried(tmp_path):
    sends = []

    def boom(xid, k, role):
        sends.append(xid)
        raise ConnectionError("reset")

    w = F.World(detail=boom)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 3 and rep["status"] == "stopped" and rep["reason"] == "consecutive_failures"
    assert len(sends) == 2 == rep["ledger"]["detail_calls"] and len(set(sends)) == 2     # 每篇只发一次
    assert [c["status"] for c in rep["calls"]] == ["network", "network"]
    assert rep["balance"]["after_read"] == "ok"                                             # 停了照样读余额


# ── 3. 状态词 ──

@pytest.mark.parametrize("status,word", [(401, "auth"), (403, "auth"), (402, "quota"), (429, "rate_limited"),
                                         (400, "bad_request"), (422, "bad_request"), (404, "not_found"),
                                         (503, "server"), (418, "bad_request"), (302, "unknown_shape")])
def test_http_status_words(status, word):
    assert K.classify(status, b"")[0] == word


@pytest.mark.parametrize("body,word", [
    (b"not json", "unknown_shape"),
    (b"[1, 2]", "unknown_shape"),
    (json.dumps({"code": 402, "data": {}}).encode(), "quota"),
    (json.dumps({"code": 401}).encode(), "auth"),
    (json.dumps({"code": 1001, "data": {}}).encode(), "upstream_failed"),
    (json.dumps({"code": 200, "success": False}).encode(), "upstream_failed"),
    (json.dumps(F.UPSTREAM).encode(), "upstream_failed"),                     # success=false + data=[]：不是「删了」
    (json.dumps({"code": 200, "data": {"success": False, "data": []}}).encode(), "upstream_failed"),
    (json.dumps(F.GONE).encode(), "gone"),
    (json.dumps({"code": 200, "data": {"code": 0, "data": [{"note_list": []}]}}).encode(), "gone"),
    (json.dumps({"code": 200, "data": {"code": 0, "data": [{"model_type": "note"}]}}).encode(), "unknown_shape"),
    (json.dumps({"code": 200, "data": {"code": 0, "data": {"x": 1}}}).encode(), "unknown_shape"),
    (json.dumps({"code": 200, "data": "x"}).encode(), "unknown_shape"),
    (json.dumps({"code": 200, "data": {"code": 0, "data": [{"note_list": ["x"]}]}}).encode(), "unknown_shape"),
    (json.dumps(F.HIDDEN).encode(), "hidden"),
    (json.dumps(F.ok_body("a" * 24, [F.item(0, 900, 1200)])).encode(), "ok"),
])
def test_body_status_words(body, word):
    assert K.classify(200, body)[0] == word


def test_transport_errors_are_network_and_http_errors_use_only_the_code():
    def raiser(exc):
        def f(*a, **k):
            raise exc
        return f
    for exc in (urllib.error.URLError("x"), TimeoutError(), ConnectionResetError(), OSError()):
        assert P.tikhub_detail(raiser(exc), "k", "a" * 24) == ("network", None)
    e = urllib.error.HTTPError("https://x.invalid/?note_id=" + "b" * 24, 429, "slow", None, None)
    assert P.tikhub_detail(raiser(e), "k", "a" * 24) == ("rate_limited", None)
    big = lambda *a, **k: P.Resp(200, {}, b"", "too_large")
    assert P.tikhub_detail(big, "k", "a" * 24)[0] == "unknown_shape"


def test_detail_request_shape():
    seen = []

    def fetch(url, headers, **kw):
        seen.append((url, headers, kw))
        return P.Resp(200, {}, json.dumps(F.GONE).encode(), "")
    P.tikhub_detail(fetch, "KEY", "c" * 24)
    url, headers, kw = seen[0]
    assert url == f"https://api.tikhub.io{P.DETAIL_PATH}?note_id={'c' * 24}"
    assert headers == {"Authorization": "Bearer KEY", "Accept": "application/json", "User-Agent": P.E.USER_AGENT}
    assert kw["timeout"] == 60 and kw["cap"] == 4 * 1024 * 1024 and kw.get("method", "GET") == "GET"


# ── 4. 停止规则 ──

def _feed(words, absent=()):
    r = K.StopRule()
    for i, w in enumerate(words, 1):
        stop = r.after(w, i in absent)
        if stop:
            return i, stop
    return len(words), None


@pytest.mark.parametrize("words,want", [
    (["gone", "gone", "gone", "ok", "gone", "hidden", "hidden"], (7, None)),
    (["server", "server", "ok"], (2, "consecutive_failures")),
    (["ok", "network", "ok", "network", "ok", "network"], (6, None)),
    (["server", "gone", "server"], (3, "consecutive_failures")),        # gone 不清零
    (["unknown_shape", "ok"], (1, "unknown_shape_call1")),
    (["ok", "unknown_shape", "ok"], (3, None)),
    (["ok", "auth"], (2, "auth")), (["quota"], (1, "quota")), (["ok", "ok", "rate_limited"], (3, "rate_limited")),
    (["upstream_failed", "hidden", "bad_request"], (3, "consecutive_failures")),
])
def test_stop_rules(words, want):
    assert _feed(words) == want


def test_two_ok_posts_without_an_image_list_stop_the_run():
    assert _feed(["ok", "ok", "ok"], absent={1, 3}) == (3, "shape_absent")


# ── 5. 选样 ──

def test_long_link_and_cell_ids():
    h = "0123456789abcdef01234567"
    assert K.id_from_long_link(f"https://www.xiaohongshu.com/explore/{h}?xsec_token=x") == (h, "long_explore")
    assert K.id_from_long_link(f"https://www.xiaohongshu.com/discovery/item/{h.upper()}") == (h, "long_discovery")
    assert K.id_from_long_link(f"https://www.xiaohongshu.com/explore/{h}a") == (None, None)       # 25 位不认
    assert K.id_from_long_link("https://xhslink.invalid/abc") == (None, None)
    link = "https://xhslink.invalid/o/abc"
    assert K.id_from_cell(f"{h} ← {link}", link + "/") == h
    assert K.id_from_cell(f" {h.upper()}  ←  {link}/ ", link) == h
    assert K.id_from_cell(f"{h} ← https://xhslink.invalid/o/old", link) is None            # 旧链接解出来的不认
    assert K.id_from_cell("1234567 ← " + link, link) is None                                 # 不是 24 位十六进制
    assert K.id_from_cell([{"text": f"{h} ← "}, {"type": "url", "text": link, "link": link}], link) == h   # 富文本段
    assert K.id_from_cell(json.dumps([{"text": f"{h} ← {link}"}]), link) == h                # PostgREST ->> 给的 JSON 文本


def test_first_valid_does_not_let_a_bad_top_level_value_hide_a_good_one():
    h = "0123456789abcdef01234567"
    link = "https://xhslink.invalid/z"
    assert K.first_valid(("不是 id", f"{h} ← {link}"), lambda v: K.id_from_cell(v, link)) == h
    assert K.first_valid(("", "正常"), K.patrol_of) == "正常"
    assert K.first_valid((None, True), K.alive_of) is True and K.first_valid(("x", "false"), K.alive_of) is False


def _norm(t):
    return [P._norm_json(r) for r in t["notes"]]


def _pools(t, excluded=(), vanished=()):
    src = {r["note_id"]: r for r in t["v_note_first_image_source"]}
    fi = {r["note_id"]: r for r in t["note_first_image"]}
    return K.build_pools(_norm(t), src, fi, set(excluded), set(vanished))


def test_default_pools_and_funnel():
    t = F.tables()
    pools, f = _pools(t)
    assert f["hits"] == 322 and f["without_feishu_image"] == 310 and f["with_feishu_image"] == 12
    assert f["targets"]["long_explore"] == 300 and f["targets"]["short_with_id"] == 5 and f["targets"]["short_no_id"] == 5
    assert f["targets"]["eligible"] == 305 and f["controls"]["eligible"] == 12
    assert f["controls"]["single"] == 5 and f["controls"]["multi"] == 7
    assert len(pools["CS"]) == 5 and len(pools["CM"]) == 7 and len(pools["TL"]) == 300 and len(pools["TS"]) == 5
    assert K.drift_word(f) == "ok"


def test_selection_exclusions_are_each_counted():
    t = F.tables(n_single=2, n_multi=0, n_long=3, n_short_id=0, n_short_no=0)
    c0, c1 = t["notes"][0], t["notes"][1]
    t["notes"].append(F.note_row("douyin", platform="douyin", url="https://v.douyin.invalid/x"))
    t["notes"].append(F.note_row("lowtier", tier="中", url="https://www.xiaohongshu.com/explore/" + "d" * 24))
    pools, f = _pools(t)
    assert f["hits"] == 6 and f["hits_xhs"] == 5 and f["without_feishu_image"] == 4 and f["without_feishu_image_xhs"] == 3
    # 每一种排除各来一篇
    variants = {
        "excluded_project": lambda: _pools(t, excluded={"PA"}),
        "vanished": lambda: _pools(t, vanished={c0["note_id"], c1["note_id"]}),
    }
    assert variants["excluded_project"]()[1]["controls"]["excluded_project"] == 2
    assert variants["vanished"]()[1]["controls"]["vanished"] == 2
    t2 = F.tables(n_single=6, n_multi=0, n_long=3, n_short_id=0, n_short_no=0)
    fi = t2["note_first_image"]
    fi[0]["file_sha"] = "f" * 64                                  # 飞书换了图
    fi[1]["status"] = "not_a_cover"
    fi[2]["width"] = None
    t2["notes"][3]["pt"] = "异常"
    t2["notes"][4]["at"] = False
    t2["note_first_image"] = [r for r in fi if r["note_id"] != t2["notes"][5]["note_id"]]
    _, f2 = _pools(t2)
    c = f2["controls"]
    assert (c["sha_changed"], c["not_answered"], c["not_a_cover_seen"], c["no_dims"], c["not_alive"], c["no_fi_row"]) == (1, 1, 1, 1, 2, 1)
    assert c["eligible"] == 0


def test_patrol_and_alive_read_from_either_place():
    t = F.tables(n_single=2, n_multi=0, n_long=3, n_short_id=0, n_short_no=0)
    t["notes"][0].update(pt=None, pu="正常", at=None, au=True)
    t["notes"][1].update(pt="", pu="正常", at="x", au=True)
    assert _pools(t)[1]["controls"]["eligible"] == 2


def test_id_conflict_and_duplicates_are_excluded():
    t = F.tables(n_single=1, n_multi=0, n_long=2, n_short_id=0, n_short_no=0)
    tg = t["notes"][1]
    other = "e" * 24
    tg["wu"] = f"{other} ← {tg['publish_url']}"                   # 长链 id 和作品ID 对不上
    dup = t["notes"][2]
    t["notes"].append(F.note_row("dup", url=dup["publish_url"], pt=None, at=None))
    _, f = _pools(t)
    assert f["targets"]["id_conflict"] == 1 and f["targets"]["id_dup"] == 2 and f["targets"]["eligible"] == 0


def test_short_links_without_an_id_are_counted_never_selected():
    t = F.tables(n_single=1, n_multi=0, n_long=0, n_short_id=0, n_short_no=4)
    pools, f = _pools(t)
    assert f["targets"]["short_no_id"] == 4 and not pools["TL"] and not pools["TS"]


def test_strata():
    assert K._strata(1080, 1920) == (True, False)
    assert K._strata(900, 1200) == (False, True)
    assert K._strata(1000, 1000) == (False, False)
    assert K._strata(1920, 1080)[0] is True                       # 宽图也算「长」


# ── 6. 槽位与顺序 ──

def test_rank_is_tv_rank_construction_with_its_own_seed():
    assert K.rank("P_rec1") == hashlib.md5(("P_rec1" + "i35-cover-pilot-1").encode("utf-8")).hexdigest()
    assert K.rank("P_rec1") == K.rank("P_rec1")


def test_slot_order_and_alternating_strata():
    pools, _ = _pools(F.tables())
    plan = K.plan_slots(pools)
    s = plan["slots"]
    assert [K.slot_label(x, s.get(x)).split()[1] for x in K.CALL_ORDER] == \
        ["control", "control", "target", "control", "target", "control", "target", "control", "control", "flex"]
    assert [s[x]["stratum"].split("_")[0] for x in ("C1", "C2", "C3", "C4")] == ["single"] * 4
    assert [s[x]["stratum"].split("_")[0] for x in ("C5", "C6")] == ["multi"] * 2
    assert s["C1"]["tall"] and not s["C2"]["tall"]                # 高图和非高图交替，高图先
    assert s["T2"]["link"] == "short_with_id"
    assert len(plan["control_reserves"]) == 6 and plan["control_reserves"][0]["single"]
    assert sum(1 for x in K.CALL_ORDER if x != "F10" and s.get(x)) + 1 <= K.MAX_CALLS
    again = K.plan_slots(_pools(F.tables())[0])
    assert [again["slots"][x]["tv"] for x in s] == [s[x]["tv"] for x in s]


def test_singles_short_are_filled_from_multi_and_t2_falls_back_to_long():
    pools, _ = _pools(F.tables(n_single=2, n_multi=7, n_short_id=0, n_short_no=10))
    s = K.plan_slots(pools)["slots"]
    assert [s[x]["single"] for x in ("C1", "C2", "C3", "C4", "C5", "C6")] == [True, True, False, False, False, False]
    assert s["T2"]["link"] == "long_explore"


def test_flex_slot_rule():
    plan = K.plan_slots(_pools(F.tables())[0])
    good = {"status": "ok", "path": K.IMAGES_PATH, "aspect": {"category": "item0_unique_match"}}
    assert K.flex_choice([good] * 6, plan)["pool"] == "target"
    assert K.flex_choice([good] * 5 + [{"status": "gone"}], plan)["pool"] == "control"
    assert K.flex_choice([good] * 5 + [dict(good, aspect={"category": "no_dims"})], plan)["pool"] == "control"
    assert K.flex_choice([good] * 5 + [dict(good, analysis_error="KeyError")], plan)["pool"] == "control"
    assert K.flex_choice([good] * 5 + [dict(good, path="absent")], plan)["pool"] == "control"


def test_a_full_run_sends_controls_and_targets_in_the_fixed_order(tmp_path):
    w = F.World()
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 0 and rep["status"] == "completed" and rep["reason"] == "done"
    assert [c["slot"] for c in rep["calls"]] == list(K.CALL_ORDER)
    assert [c["pool"] for c in rep["calls"]] == ["control", "control", "target", "control", "target", "control",
                                                 "target", "control", "control", "target"]
    assert rep["ledger"]["detail_calls"] == 10 and rep["ledger"]["yuan"] == 0.72 and len(w.kinds("detail")) == 10
    assert len({x[1] for x in w.kinds("detail")}) == 10
    assert all(x[2]["User-Agent"] == P.E.USER_AGENT for x in w.kinds("detail"))
    assert rep["scrubbed"] == 0


def test_flex_uses_a_control_reserve_when_a_control_failed(tmp_path):
    def detail(xid, k, role):
        return F.GONE if k == 2 else F.World.default_detail(w, xid, k, role)
    w = F.World(detail=detail)
    code, rep = F.run(w, tmp=tmp_path)
    assert rep["calls"][1]["status"] == "gone" and rep["calls"][-1]["slot"] == "F10" and rep["calls"][-1]["pool"] == "control"
    assert rep["ledger"]["detail_calls"] == 10


# ── 7. 形状 ──

@pytest.mark.parametrize("k,ok", [("images_list", True), ("url_size_large", True), ("a" * 24, False), ("deadbeef", False),
                                  ("note1234567", False), ("https", True), ("x.y", False), ("标题", False),
                                  ("Title", False), ("a" * 41, False), ("k_" + "0123456789ab", False)])
def test_safe_key(k, ok):
    assert K.safe_key(k) is ok


def test_shape_records_names_and_types_only():
    sh = K.Shape()
    secret_note_key = "5f3a" + "0" * 20
    items = [F.item(0, 900, 1200), F.item(1, 900, 1200)]
    items[0]["live_photo"] = {"url": "https://video.invalid/x"}
    raw = F.ok_body("a" * 24, items, note_extra={secret_note_key: 1, "标题": "x", "cover": {"url": "u"},
                                                "video_info_v2": {"first_frame": "f"}},
                    envelope_extra={"Odd-Key": 1})
    info = sh.add(raw, K.note_of(raw))
    assert info == {"path": K.IMAGES_PATH, "v1_ok": True, "v1_fail": None, "is_video": True, "note_type": "normal",
                    "model_type": "note"}
    assert set(sh.keys["envelope"]) == {"code", "router", "params", "cache_url", "data"}
    assert "note_id" not in json.dumps({k: dict(v) for k, v in sh.keys.items()})     # params 里面不走
    assert secret_note_key not in sh.keys["note"] and sh.odd >= 3
    assert sh.item0_types["url"] == {"str_url": 1} and sh.item0_types["index"] == {"int": 1}
    assert sh.item0_subkeys["origin_img"] == {"width": 1, "height": 1, "size": 1}
    assert {"cover", "video_info_v2.first_frame", "images_list"} <= set(sh.cover_like)
    assert sh.cover_like["cover"] == {"dict": 1} and "cover.url" not in sh.cover_like     # 只记名字像封面的叶子
    assert "video_info_v2.first_frame" in sh.video_cover
    blob = json.dumps([dict(sh.item_keys), {k: dict(v) for k, v in sh.item0_types.items()}])
    assert "://" not in blob and "1080" not in blob


def test_shape_caps():
    sh = K.Shape()
    note = {f"k{i}": i for i in range(200)}
    note["images_list"] = [{f"f{i}": 1 for i in range(100)}]
    raw = {"code": 200, "data": {"code": 0, "data": [{"note_list": [note]}]}}
    sh.add(raw, note)
    assert len(sh.keys["note"]) == 80 and len(sh.item_keys) == 40 and sh.truncated > 0


@pytest.mark.parametrize("item0,fail", [({"index": 0, "width": 1, "height": 1}, "no_url"),
                                        ({"width": 1, "height": 1, "url": "https://a.invalid/x"}, "no_index"),
                                        ({"index": 0, "height": 1, "url": "http://a.invalid/x"}, "no_width")])
def test_v1_per_post_failures(item0, fail):
    raw = F.ok_body("a" * 24, [item0])
    assert K.Shape().add(raw, K.note_of(raw))["v1_fail"] == fail


def test_http_image_links_count_for_v1():
    raw = F.ok_body("a" * 24, [{"index": 0, "width": 1, "height": 1, "url": "http://a.invalid/x"}])
    assert K.Shape().add(raw, K.note_of(raw))["v1_ok"] is True


# ── 8. 顺序 ──

@pytest.mark.parametrize("idx,word", [([0, 1, 2], "sequential_0"), ([1, 2, 3], "sequential_1"), ([0, 2, 5], "sorted_gaps"),
                                      ([1, 0, 2], "unsorted"), ([0, None], "non_int"), (["0", "1"], "sequential_0"),
                                      ([0, 0, 0], "ties"), ([0, 1, 1], "ties"), ([0, 2, 1], "unsorted"),
                                      ([0.5, 1], "non_int"), ([0], "single"), ([], "na")])
def test_index_order(idx, word):
    assert K.index_order([{"index": i} for i in idx]) == word


def test_index_order_missing():
    assert K.index_order([{"index": 0}, {"url": "x"}]) == "missing"


# ── 9. 宽高比 ──

def _items(*dims):
    return [{"width": w, "height": h} for w, h in dims]


def test_aspect_categories():
    tall, std = (1080, 1920), (900, 1200)
    assert K.aspect_eval(_items(tall, std), 540, 960)["category"] == "item0_unique_match"
    assert K.aspect_eval(_items(std, std), 450, 600)["category"] == "item0_shared_match"
    assert K.aspect_eval(_items(std, tall), 540, 960)["category"] == "other_match_only"
    assert K.aspect_eval(_items(std, std), 1000, 1000)["category"] == "no_match"
    a = K.aspect_eval(_items(tall), 540, 960)
    assert a["category"] == "single_image_match" and a["d0"] == 0.0
    a = K.aspect_eval(_items(std), 540, 960)                    # 只有一张、对不上 (比如长图被裁成 3:4): 不再一律算「一张图」
    assert a["category"] == "single_image_no_match" and a["d0"] > K.ASPECT_TOL
    assert K.aspect_eval(_items(std), None, 960)["category"] == "no_dims"
    assert K.aspect_eval([{"url": "x"}, {"width": 1, "height": 1}], 1, 1)["category"] == "no_dims"


def test_aspect_tolerance_boundary_and_flags():
    r = 4 / 3
    w = 10000
    near = round(w * r * math.exp(0.0099))
    far = round(w * r * math.exp(0.0105))
    assert K.aspect_eval(_items((w, near), (900, 1200)), 900, 1200)["d0"] <= K.ASPECT_TOL
    assert K.aspect_eval(_items((w, far), (900, 1200)), 900, 1200)["category"] == "other_match_only"
    a = K.aspect_eval(_items((1080, 1920), (900, 1200), (900, 1200)), 540, 960)
    assert a["discriminating"] and a["best_index"] == "0"
    tie = K.aspect_eval(_items((900, 1200), (900, 1200)), 900, 1200)
    assert tie["best_index"] == "0" and not tie["discriminating"]
    assert K.aspect_eval(_items((900, 1200), (900, 1200), (900, 1200), (1080, 1920)), 540, 960)["best_index"] == "3_plus"


def test_aspect_falls_back_to_origin_img_and_flags_mismatch():
    items = [{"origin_img": {"width": 1080, "height": 1920}}, {"width": 900, "height": 1200}]
    a = K.aspect_eval(items, 540, 960)
    assert a["dims_source"] == "origin_img" and a["category"] == "item0_unique_match"
    b = K.aspect_eval([{"width": 900, "height": 1200, "origin_img": {"width": 1080, "height": 1920}}], 900, 1200)
    assert b["origin_vs_item"] == "mismatch"
    assert K.aspect_eval([{"width": "900", "height": "1200"}], 900, 1200)["d0"] == 0.0       # 字符串宽高也认


def test_size_match_is_exploratory():
    items = [{"origin_img": {"size": 10}}, {"origin_img": {"size": 20}}]
    assert K.size_match_index(items, 20) == "1" and K.size_match_index(items, 30) == "none"
    assert K.size_match_index(items, None) == "no_size" and K.size_match_index([{}], 5) == "no_size"


# ── 10. 视频 / 选链接 ──

def test_choose_url_never_falls_into_live_photo_or_video():
    item = {"live_photo": {"url": "https://video.invalid/l"}, "video": "https://video.invalid/v",
            "url_multi_level": {"high": "https://img.invalid/h"}, "url": "  "}
    assert K.choose_url(item) == (None, None)
    item["original"] = "http://img.invalid/o"
    assert K.choose_url(item) == ("http://img.invalid/o", "original")
    item["url_size_large"] = "https://img.invalid/l"
    assert K.choose_url(item) == ("https://img.invalid/l", "url_size_large")
    assert K.choose_url(None) == (None, None) and K.choose_url({"url": "ftp://x"}) == (None, None)


def test_video_note_counts_and_cover_keys(tmp_path):
    def detail(xid, k, role):
        if k == 1:
            return F.ok_body(xid, [F.item(0, 1080, 1920)], note_extra={"type": "video", "video_info_v2": {"cover": "c"}})
        return F.World.default_detail(w, xid, k, role)
    w = F.World(detail=detail)
    _, rep = F.run(w, tmp=tmp_path)
    assert rep["video"]["video_notes"] == 1 and rep["video"]["type_counts"]["video"] == 1
    assert "video_info_v2.cover" in rep["video"]["video_cover_keys"]
    assert rep["calls"][0]["download"]["video"] is True


def test_video_content_type_is_stopped_before_the_body():
    calls = []

    def fetch(url, headers, **kw):
        calls.append(kw["stop_ct"]("video/mp4"))
        return P.Resp(200, {"content-type": "video/mp4"}, b"", "ct_stopped")
    d = P.Downloader(K.Ledger(), fetch, lambda s: None, lambda: 0.0)
    out = d.download({"url": "https://img.xhsfake.invalid/a", "key": "url", "dims": (1, 1)}, "C1")
    assert calls[0] is True and out["probes"][0]["status"] == "not_image"


# ── 11. 链接闸 ──

@pytest.mark.parametrize("url,ok,why", [
    ("https://img.xhsfake.invalid/a?x=1", True, ""), ("http://img.xhsfake.invalid/a", True, ""),
    ("http://img.xhsfake.invalid:80/a", True, ""), ("https://img.xhsfake.invalid:443/a", True, ""),
    ("ftp://img.invalid/a", False, "scheme"), ("file:///etc/passwd", False, "scheme"),
    ("https://127.0.0.1/", False, "ip"), ("https://[::1]/", False, "ip"), ("https://2130706433/", False, "ip"),
    ("https://0x7f000001/", False, "ip"), ("https://127.1/", False, "ip"), ("https://u:p@img.invalid/", False, "userinfo"),
    ("https://img.invalid:8443/", False, "port"), ("http://img.invalid:443/", False, "port"),
    ("https://localhost/", False, "host"), ("https://intranet/", False, "host"), ("https://a.b.c1/", False, "host"),
    ("https://img.invalid:x/", False, "port"),
])
def test_url_gate(url, ok, why):
    assert K.url_gate(url) == (ok, why)


def test_registrable_and_xhs_named():
    assert K.registrable("sns-img-a.xhscdn.com") == "xhscdn.com"
    assert K.registrable("a.b.example.com.cn") == "example.com.cn"
    assert K.xhs_named("sns-a.xhsfake.invalid") and K.xhs_named("x.rednotecdn.invalid")
    assert not K.xhs_named("cdn.example.com") and not K.xhs_named("myxhs.example.com")


# ── 12. 下载 ──

class ImgFetch:
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, url, headers, **kw):
        self.calls.append((url, dict(headers)))
        out = self.script(url, headers, len(self.calls))
        if isinstance(out, BaseException):
            raise out
        if kw.get("stop_ct") and kw["stop_ct"](out.headers.get("content-type")):
            return P.Resp(out.status, out.headers, b"", "ct_stopped")
        return out


def _dl(script, ledger=None):
    f = ImgFetch(script)
    return P.Downloader(ledger or K.Ledger(), f, lambda s: None, lambda: 0.0), f


def _plan(url="https://img.xhsfake.invalid/w900h1200", dims=(900, 1200)):
    return {"url": url, "key": "url_size_large", "dims": dims, "video": False}


OKW = P.Resp(200, {"content-type": "image/webp"}, F.webp(900, 1200), "")


def test_cross_domain_redirect_is_not_followed():
    d, f = _dl(lambda u, h, k: P.Resp(302, {"location": "https://evil.example.invalid/x"}, b"", ""))
    out = d.download(_plan(), "C1")
    assert out["probes"][0]["status"] == "redirect" and len([c for c in f.calls if "evil" in c[0]]) == 0
    assert len(f.calls) == 3                                         # 每档一次，一次都没跟


def test_same_domain_redirect_is_followed_at_most_twice():
    def script(u, h, k):
        if k == 1:
            return P.Resp(302, {"location": "/w900h1200?b=1"}, b"", "")
        return OKW
    d, f = _dl(script)
    out = d.download(_plan(), "C1")
    assert out["status"] == "ok" and out["redirected"] == 1 and len(f.calls) == 2
    loop, g = _dl(lambda u, h, k: P.Resp(301, {"location": "https://other.xhsfake.invalid/again"}, b"", ""))
    assert loop.download(_plan(), "C1")["probes"][0]["status"] == "redirect"
    assert len(g.calls) == 9                                         # 3 档 × (1 + 2 跳)


def test_tier_probe_finds_and_reuses_the_working_tier():
    def script(u, h, k):
        return OKW if "Referer" in h else P.Resp(403, {"content-type": "text/html"}, b"", "")
    d, f = _dl(script)
    first = d.download(_plan(), "C1")
    assert [p["status"] for p in first["probes"]] == ["forbidden", "forbidden", "ok"]
    assert first["tier"] == "browser_ua_referer" and d.tier_needed == "browser_ua_referer"
    assert f.calls[0][1] == {"Accept": K.IMAGE_ACCEPT}
    assert f.calls[1][1]["User-Agent"] == K.BROWSER_UA and "Referer" not in f.calls[1][1]
    n = len(f.calls)
    second = d.download(_plan(), "C2")
    assert second["status"] == "ok" and second["probes"] == [] and len(f.calls) == n + 1
    assert all("Authorization" not in c[1] and "Cookie" not in c[1] for c in f.calls)


def test_one_refused_post_does_not_disable_downloads_two_do():
    d, f = _dl(lambda u, h, k: P.Resp(403, {}, b"", ""))
    d.download(_plan(), "C1")
    assert not d.disabled and len(f.calls) == 3
    d.download(_plan(), "C2")
    assert d.disabled and len(f.calls) == 6
    third = d.download(_plan(), "C3")
    assert third["status"] == "not_attempted" and third["not_attempted_reason"] == "disabled" and len(f.calls) == 6


def test_network_and_server_errors_never_disable_and_the_next_post_reprobes():
    seq = iter([TimeoutError(), P.Resp(503, {}, b"", ""), P.Resp(403, {}, b"", ""), OKW])
    d, f = _dl(lambda u, h, k: next(seq))
    assert d.download(_plan(), "C1")["status"] == "network" and len(f.calls) == 1
    assert d.download(_plan(), "C2")["status"] == "server" and len(f.calls) == 2
    third = d.download(_plan(), "C3")
    assert third["status"] == "ok" and third["tier"] == "browser_ua" and not d.disabled and d.refused_posts == 0


def test_placeholder_image_is_not_ok_and_escalates():
    def script(u, h, k):
        return OKW if "User-Agent" in h else P.Resp(200, {"content-type": "image/png"}, F.png(100, 100), "")
    d, _ = _dl(script)
    out = d.download(_plan(), "C1")
    assert [p["status"] for p in out["probes"]] == ["placeholder_suspect", "ok"] and out["tier"] == "browser_ua"


def test_http_links_try_https_first_then_http():
    def script(u, h, k):
        return P.Resp(200, {"content-type": "image/webp"}, F.webp(900, 1200), "") if u.startswith("http://") \
            else TimeoutError()
    d, f = _dl(script)
    out = d.download(_plan("http://img.xhsfake.invalid/w900h1200"), "C1")
    assert [c[0].split(":")[0] for c in f.calls] == ["https", "http"]
    assert out["status"] == "ok" and out["scheme"] == "http" and out["scheme_used"] == "http"
    later = d.download(_plan("http://img.xhsfake.invalid/w900h1200"), "C2")
    assert later["status"] == "ok" and f.calls[-1][0].startswith("http://") and len(f.calls) == 3


def test_image_request_cap_is_enforced():
    L = K.Ledger()
    d, f = _dl(lambda u, h, k: P.Resp(500, {}, b"", ""), ledger=L)
    for i in range(K.MAX_IMAGE_REQUESTS + 5):
        d.download(_plan(), f"S{i}")
    assert len(f.calls) == K.MAX_IMAGE_REQUESTS == L.image_requests
    assert d.download(_plan(), "Z")["not_attempted_reason"] == "image_cap"


def test_url_rejected_and_no_url():
    d, f = _dl(lambda u, h, k: OKW)
    assert d.download(_plan("https://127.0.0.1/a"), "C1") ["status"] == "url_rejected"
    assert d.download(_plan(None), "C2")["status"] == "no_url" and not f.calls


@pytest.mark.parametrize("head,fmt", [(F.webp(2, 3), "webp"), (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "jpeg"),
                                      (F.png(2, 3), "png"), (b"GIF89a" + b"\x00" * 10, "gif"),
                                      (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8, "heic"),
                                      (b"\x00\x00\x00\x18ftypavif" + b"\x00" * 8, "avif"), (b"<html>", "other")])
def test_sniff(head, fmt):
    assert K.sniff(head) == fmt


def test_dims_from_bytes():
    assert K.dims_from_bytes(F.png(640, 480)) == (640, 480)
    assert K.dims_from_bytes(F.webp(1080, 1440)) == (1080, 1440)
    vp8l = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 4 + b"\x2f" + ((300 - 1) | ((200 - 1) << 14)).to_bytes(4, "little") + b"\x00" * 8
    assert K.dims_from_bytes(vp8l) == (300, 200)
    vp8 = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 4 + b"\x00\x00\x00\x9d\x01\x2a" + (64).to_bytes(2, "little") + (48).to_bytes(2, "little")
    assert K.dims_from_bytes(vp8) == (64, 48)
    jpeg = (b"\xff\xd8" + b"\xff\xe0" + (16).to_bytes(2, "big") + b"\x00" * 14
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + (1440).to_bytes(2, "big") + (1080).to_bytes(2, "big") + b"\x00" * 10)
    assert K.dims_from_bytes(jpeg) == (1080, 1440)
    assert K.dims_from_bytes(b"\xff\xd8\xff") is None and K.dims_from_bytes(b"junk") is None
    assert K.bytes_vs_json(F.webp(1440, 1080), (1080, 1440)) == "match"          # 转了 90° 也算对得上
    assert K.bytes_vs_json(F.png(10, 10), (900, 1200)) == "mismatch"
    assert K.bytes_vs_json(b"\x00\x00\x00\x18ftypheic", (1, 1)) == "unparsed"


@pytest.mark.parametrize("n,b", [(0, "lt_50k"), (49_999, "lt_50k"), (50_000, "50k_200k"), (999_999, "200k_1m"),
                                 (1_000_000, "1m_5m"), (5_000_000, "gt_5m")])
def test_byte_buckets(n, b):
    assert K.byte_bucket(n) == b


def test_ct_family():
    assert [K.ct_family(x) for x in ("image/webp", "Text/HTML; charset=x", "video/mp4", "application/json", "", None, "font/x")] \
        == ["image", "text", "video", "application", "missing", "missing", "other"]


class _FakeHTTPResp:
    def __init__(self, status, headers, chunks):
        self.status, self.headers, self._chunks = status, headers, list(chunks)
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


def test_http_fetch_caps_by_content_length_and_by_streaming(monkeypatch):
    seen = []

    def opener(resp):
        def open_(req, timeout=None):
            seen.append(req)
            return resp
        return open_
    from email.message import Message
    h = Message(); h["Content-Length"] = str(100); h["Content-Type"] = "image/webp"
    r = _FakeHTTPResp(200, h, [b"x" * 100])
    monkeypatch.setattr(P._OPENER, "open", opener(r))
    assert P.http_fetch("https://a.invalid/x", {}, timeout=1, cap=50).flag == "too_large" and r.closed
    h2 = Message(); h2["Content-Type"] = "image/webp"
    r2 = _FakeHTTPResp(200, h2, [b"x" * 30, b"x" * 30, b"x" * 30])
    monkeypatch.setattr(P._OPENER, "open", opener(r2))
    assert P.http_fetch("https://a.invalid/x", {}, timeout=1, cap=50).flag == "too_large"
    r3 = _FakeHTTPResp(200, h2, [b"x" * 30])
    monkeypatch.setattr(P._OPENER, "open", opener(r3))
    got = P.http_fetch("https://a.invalid/x", {"A": "b"}, timeout=1, cap=50)
    assert got.body == b"x" * 30 and got.status == 200 and got.flag == ""
    assert all(req.get_method() == "GET" and req.data is None for req in seen)


def test_http_fetch_turns_http_errors_into_status_without_raising(monkeypatch):
    from email.message import Message
    hdrs = Message(); hdrs["Location"] = "https://x.invalid/next"

    def open_(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", hdrs, io.BytesIO(b""))
    monkeypatch.setattr(P._OPENER, "open", open_)
    r = P.http_fetch("https://a.invalid/x", {}, timeout=1, cap=10)
    assert r.status == 302 and r.headers["location"] == "https://x.invalid/next" and r.body == b""


def test_the_opener_never_follows_redirects():
    h = P._NoRedirect()
    assert h.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid/") is None
    assert any(isinstance(x, P._NoRedirect) for x in P._OPENER.handlers)
    assert not any(type(x).__name__ == "HTTPCookieProcessor" for x in P._OPENER.handlers)


# ── 13. 链接形状 ──

def test_url_shape_names_only():
    s = K.url_shape("https://img.xhsfake.invalid/abcdef0123456789abcdef0123456789?sign=0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d"
                    "&t=6735a8f0&ap=17&sc=2&imageView2/2/w/1080/format/webp")
    assert s["param_names"] == ["sign", "t", "ap", "sc"] and s["odd_params"] == 1 and s["expiry_like"]
    assert s["expiry_shapes"] == {"sign": "hex_17_32", "t": "hex_1_8"} and s["path_segments"] == 1
    assert "0a1b" not in json.dumps(s) and "6735" not in json.dumps(s)
    assert not K.url_shape("https://a.invalid/x?w=1")["expiry_like"]
    assert K.value_shape("1700000000") == "digits_9_16" and K.value_shape("a+b/c==") == "b64ish_1_8"


# ── 14. 复查 ──

def test_recheck_reuses_the_tier_and_compares_bytes(tmp_path):
    n = {"k": 0}

    def image(url, headers, k):
        if "User-Agent" not in headers:
            return P.Resp(403, {}, b"", "")
        n["k"] += 1
        body = F.webp(900, 1200) if n["k"] != 9 else F.webp(900, 1200) + b"changed"
        return P.Resp(200, {"content-type": "image/webp"}, body, "")

    def detail(xid, k, role):
        return F.ok_body(xid, [F.item(0, 900, 1200), F.item(1, 900, 1200)])
    w = F.World(detail=detail, image=image)
    code, rep = F.run(w, tmp=tmp_path, recheck="20")
    assert code == 0 and 20 * 60 in w.sleeps
    rechecks = [c for c in rep["calls"] if c["recheck"] == "ok"]
    assert len(rechecks) == 10 and rep["q3_download"]["recheck_same_bytes"] == {"yes": 9, "no": 1}
    assert rep["verdicts"]["q3_recheck"] == "stable_bytes_changed"
    after = w.kinds("image")[-10:]
    assert all(x[2].get("User-Agent") == K.BROWSER_UA and "Referer" not in x[2] for x in after)


def test_recheck_skipped_when_zero_or_out_of_time(tmp_path):
    w = F.World()
    _, rep = F.run(w, tmp=tmp_path, recheck="0")
    assert {c["recheck"] for c in rep["calls"]} == {"skipped"} and rep["verdicts"]["q3_recheck"] == "skipped"
    w2 = F.World()
    w2.clock_v[0] = 0.0
    orig = w2.clock

    def late():
        v = orig()
        return v + (40 * 60 if len(w2.kinds("detail")) >= 10 else 0)
    w2.clock = late
    _, rep2 = F.run(w2, tmp=tmp_path / "b", recheck="10")
    assert {c["recheck"] for c in rep2["calls"]} == {"skipped_time"} and 600 not in w2.sleeps


# ── 15. 余额 ──

@pytest.mark.parametrize("first,reason", [
    (P.Resp(401, {}, b"", ""), "balance_auth"),
    (P.Resp(200, {}, b"not json", ""), "balance_unreadable"),
    (P.Resp(200, {}, json.dumps({"code": 200, "data": {}}).encode(), ""), "balance_unreadable"),
    (P.Resp(200, {}, json.dumps({"user_data": {"balance": 99, "account_disabled": True}}).encode(), ""), "balance_disabled"),
    (0.49, "balance_low"),
])
def test_balance_refusals_send_nothing_paid_and_do_not_claim(tmp_path, first, reason):
    w = F.World(balances=[first, 9.0])
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["status"] == "refused" and rep["reason"] == reason
    assert not w.kinds("detail") and not w.kinds("claim") and not w.kinds("image")
    assert rep["ledger"]["detail_calls"] == 0


def test_balance_nested_under_data_is_read():
    body = json.dumps({"code": 200, "data": {"user_data": {"balance": "3.5"}}}).encode()
    assert P.tikhub_balance(lambda *a, **k: P.Resp(200, {}, body, ""), "k") == ("ok", 3.5)


def test_balance_diff_is_before_minus_after_and_read_right_after_the_last_call(tmp_path):
    w = F.World(balances=[12.0, 11.85])
    code, rep = F.run(w, tmp=tmp_path)
    b = rep["balance"]
    assert b["diff_usd"] == 0.15 and b["diff_yuan"] == 1.08 and b["expected_usd"] == 0.1 and b["excess_usd"] == 0.05
    assert b["label"] == "whole_account_shared" and b["floor"] == "ok"
    kinds = [x[0] for x in w.log if x[0] != "supabase"]
    last_detail = max(i for i, k in enumerate(kinds) if k == "detail")
    assert kinds[last_detail + 1] == "balance"                     # 最后一次付费调用之后紧接着读余额
    assert kinds[last_detail + 2] == "image"                       # 最后一篇的下载在那之后
    assert kinds.index("balance") < kinds.index("claim") < kinds.index("detail")
    assert "12.0" not in json.dumps(rep) and "11.85" not in json.dumps(rep)


# ── 16. 认领 ──

@pytest.mark.parametrize("status,reason", [(422, "refused_claimed"), (403, "refused_claim_error"), (500, "refused_claim_error")])
def test_claim_refusals_send_nothing_paid(tmp_path, status, reason):
    w = F.World(claim=status)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["reason"] == reason and not w.kinds("detail") and len(w.kinds("claim")) == 1


def test_claim_payload_and_stdlib_post():
    w = F.World()
    assert P.claim_run("owner/repo", "tok", "a" * 40, fetch=w.fetch) == "claimed"
    _, method, payload, url = w.kinds("claim")[0]
    assert method == "POST" and url == "https://api.github.com/repos/owner/repo/git/refs"
    assert payload == {"ref": "refs/tags/cover-pilot-i35-run-claimed", "sha": "a" * 40}
    assert P.claim_run("owner/repo", "", "a" * 40, fetch=w.fetch) == "refused_claim_error"
    assert P.claim_run("../x", "tok", "a" * 40, fetch=w.fetch) == "refused_claim_error"
    assert P.claim_run("owner/repo", "tok", "short", fetch=w.fetch) == "refused_claim_error"

    def boom(url, payload):
        raise urllib.error.URLError("x")
    assert P.claim_run("owner/repo", "tok", "a" * 40, post=boom) == "refused_claim_error"
    assert len(w.kinds("claim")) == 1


@pytest.mark.parametrize("change,reason", [
    ({"TIKHUB_API_KEY": ""}, "refused_missing_key"),
    ({"GITHUB_TOKEN": ""}, "refused_missing_token"),
    ({"GITHUB_SHA": ""}, "refused_missing_token"),
])
def test_missing_key_or_token_refuse_before_balance_and_claim(tmp_path, change, reason):
    w = F.World()
    w.env.update(change)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["reason"] == reason
    assert not w.kinds("balance") and not w.kinds("claim") and not w.kinds("detail")


def test_crawl_window_refuses_before_anything_paid(tmp_path):
    w = F.World(now=datetime(2026, 11, 19, 3, 30, tzinfo=timezone.utc))         # 周四 03:30 UTC
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["reason"] == "refused_window" and rep["selection"]["window_now"] == "crawl_window"
    assert not w.kinds("balance") and not w.kinds("claim") and not w.kinds("detail")


def test_drift_and_pool_guards(tmp_path):
    w = F.World(F.tables(n_long=250))
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["reason"] == "refused_drift" and not w.kinds("balance")
    w2 = F.World(F.tables(n_single=2, n_multi=1))
    code2, rep2 = F.run(w2, tmp=tmp_path / "b")
    assert code2 == 2 and rep2["reason"] == "refused_pool" and not w2.kinds("balance")


# ── 17. 抓取时段 ──

@pytest.mark.parametrize("dt,inside", [
    (datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc), True), (datetime(2026, 11, 16, 12, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc), False), (datetime(2026, 11, 16, 1, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 19, 3, 7, tzinfo=timezone.utc), True), (datetime(2026, 11, 18, 3, 7, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 20, 3, 7, tzinfo=timezone.utc), False),
])
def test_crawl_window(dt, inside):
    assert K.in_crawl_window(dt) is inside


# ── 18. 结论规则（跑之前定死） ──

def _c(pool="control", status="ok", **kw):
    base = {"pool": pool, "status": status, "stratum": "single_tall", "single": True, "is_video": False,
            "path": K.IMAGES_PATH, "v1_ok": True, "n_images": 2, "index_order": "sequential_0"}
    base.update(kw)
    return base


def test_v1():
    assert K.v1_verdict([_c(), _c(), _c()])[0] == "confirmed"
    assert K.v1_verdict([_c(), _c()])[0] == "too_few"
    assert K.v1_verdict([_c(), _c(), _c(v1_ok=False)])[0] == "differs"
    assert K.v1_verdict([_c(status="gone")])[0] == "no_data"
    vid = _c(is_video=True, path="absent", v1_ok=False)
    assert K.v1_verdict([_c(), _c(), _c(), vid])[0] == "confirmed"         # 没有图片列表的视频不进分母


def test_v2a_order():
    assert K.v2a_verdict([_c(index_order="sequential_1")] * 3)[0] == "yes"
    assert K.v2a_verdict([_c(), _c(), _c()])[0] == "yes"
    assert K.v2a_verdict([_c(), _c(index_order="sequential_1"), _c()])[0] == "unknown"   # 0 起、1 起混着: 可能是第一张被丢了
    assert K.v2a_verdict([_c(), _c()])[0] == "unknown"
    assert K.v2a_verdict([_c(), _c(), _c(index_order="sorted_gaps")])[0] == "unknown"   # 缺号: 第 0 项还是最小, 不算「不是」
    assert K.v2a_verdict([_c(), _c(), _c(index_order="ties")])[0] == "unknown"          # 重号 (全是 0): 不带顺序信息
    assert K.v2a_verdict([_c(), _c(), _c(index_order="unsorted")])[0] == "no"
    assert K.v2a_verdict([_c(), _c(), _c(index_order="missing")])[0] == "unknown"
    assert K.v2a_verdict([_c(n_images=1, index_order="single")] * 3)[0] == "unknown"


def _a(cat, d0=0.0, single=True, disc=False):
    return _c(single=single, aspect={"category": cat, "d0": d0, "discriminating": disc})


def test_v2b():
    um, sm, om, nm = "item0_unique_match", "item0_shared_match", "other_match_only", "no_match"
    U = lambda **kw: _a(um, disc=True, **kw)  # noqa: E731
    assert K.v2b_verdict([U(), U(), _a(sm), _a(nm, d0=0.3, single=False)])["verdict"] == "corroborates"
    assert K.v2b_verdict([_a(om, 0.2), _a(om, 0.2), U(), U()])["verdict"] == "contradicts"
    assert K.v2b_verdict([_a(om, 0.2), U(), U()])["verdict"] == "inconclusive"        # 一篇 other_match_only 不够判「不是」
    assert K.v2b_verdict([_a(nm, 0.3), _a(nm, 0.3), U()])["verdict"] == "controls_uninformative"
    assert K.v2b_verdict([U(single=False), U(single=False), U()])["verdict"] == "inconclusive"   # 只有一篇单图
    assert K.v2b_verdict([])["verdict"] == "controls_uninformative"


def test_v2b_review_cases():
    """11-18 评审：多图对照不动结论；并列匹配 / 只有一张图不算佐证；一张图对不上算对不上。"""
    um, sm, om = "item0_unique_match", "item0_shared_match", "other_match_only"
    U = lambda **kw: _a(um, disc=True, **kw)  # noqa: E731
    # ① 4 篇单图对照能分辨地对上 + 2 篇多图对照「只对上别的项」: 多图的不推翻单图的
    r = K.v2b_verdict([U(), U(), U(), U(), _a(om, 0.2, single=False), _a(om, 0.2, single=False)])
    assert r["verdict"] == "corroborates" and r["other_match_only_multi"] == 2 and r["other_match_only_single"] == 0
    # ② 全是并列匹配 (每一项都是 3:4): 说明不了第 0 项, 不算佐证
    r = K.v2b_verdict([_a(sm)] * 6)
    assert r["verdict"] == "controls_uninformative" and r["corroborating_single"] == 0
    assert K.v2b_verdict([_a(um, disc=False)] * 3)["verdict"] == "controls_uninformative"
    # ③ 只有一张图: 对上了说明不了什么, 对不上算对不上
    assert K.v2b_verdict([_a("single_image_match")] * 3)["verdict"] == "controls_uninformative"
    r = K.v2b_verdict([U(), U()] + [_a("single_image_no_match", 0.29)] * 4)
    assert r["verdict"] == "inconclusive" and r["mismatch_single"] == 4     # 对不上的过半: 不算佐证 (原来会算 corroborates)
    r = K.v2b_verdict([U(), U(), U()] + [_a("single_image_no_match", 0.29)] * 2)
    assert r["verdict"] == "corroborates"                      # 对不上的不过半
    # ④ 两篇单图「只对上别的项」才算「不是」
    assert K.v2b_verdict([_a(om, 0.2), _a(om, 0.2)])["verdict"] == "contradicts"


def test_v2_needs_aspect_corroboration_not_only_index():
    assert K.v2_verdict("yes", "corroborates") == "yes"
    assert K.v2_verdict("yes", "inconclusive") == "unsettled"
    assert K.v2_verdict("yes", "controls_uninformative") == "unsettled"
    assert K.v2_verdict("no", "corroborates") == "no" and K.v2_verdict("yes", "contradicts") == "no"


@pytest.mark.parametrize("att,ok,dis,want", [(3, 3, False, "yes"), (10, 9, False, "yes"), (10, 8, False, "partial"),
                                             (4, 2, False, "partial"), (4, 1, False, "no"), (2, 2, False, "not_tested"),
                                             (0, 0, False, "not_tested"), (1, 0, True, "no")])
def test_v3(att, ok, dis, want):
    assert K.v3_verdict(att, ok, dis) == want


def test_recheck_verdict():
    assert K.recheck_verdict(["ok", "ok"], [True, True]) == "stable"
    assert K.recheck_verdict(["ok", "ok"], [True, False]) == "stable_bytes_changed"
    assert K.recheck_verdict(["ok", "forbidden"], [True]) == "degraded"
    assert K.recheck_verdict(["skipped"], []) == "skipped" and K.recheck_verdict([], []) == "skipped"
    # 图片请求上限用完、没去重下 (not_attempted) 不是链接过期: 不算 degraded
    assert K.recheck_verdict(["ok", "ok", "not_attempted"], [True, True]) == "stable"
    assert K.recheck_verdict(["not_attempted", "not_attempted"], []) == "cap_reached"
    assert K.recheck_verdict(["ok", "forbidden", "not_attempted"], [True]) == "degraded"


@pytest.mark.parametrize("args,want", [
    (("refused", 9, "confirmed", "yes", "yes"), ("do_not_ask_yet", "run_incomplete")),
    (("completed", 3, "confirmed", "yes", "yes"), ("do_not_ask_yet", "run_incomplete")),
    (("completed", 6, "differs", "yes", "yes"), ("do_not_ask_yet", "shape_differs")),
    (("completed", 6, "confirmed", "no", "yes"), ("do_not_ask_yet", "cover_no")),
    (("completed", 6, "confirmed", "yes", "no"), ("do_not_ask_yet", "download_blocked")),
    (("completed", 6, "confirmed", "yes", "not_tested"), ("do_not_ask_yet", "download_untested")),
    (("completed", 6, "confirmed", "unsettled", "yes"), ("do_not_ask_yet", "cover_unsettled")),
    (("stopped", 6, "confirmed", "yes", "partial"), ("ask_owner_full_run", "all_conditions_met")),
    (("planned", 0, "no_data", "unsettled", "not_tested"), ("na", "plan_only")),
])
def test_recommendation_precedence(args, want):
    assert K.recommendation(*args) == want


def test_wilson_known_values():
    assert K.wilson(0, 4) == [0.0, 0.49]
    assert K.wilson(1, 4) == [0.046, 0.699]
    assert K.wilson(4, 4) == [0.51, 1.0]


# ── 19. 单篇的怪数据不停整次运行；形状不对就停 ──

def test_odd_values_in_one_post_are_recorded_and_the_run_goes_on(tmp_path):
    def detail(xid, k, role):
        if k == 1:
            return F.ok_body(xid, [None, {"index": "x", "width": "wide"}])
        return F.World.default_detail(w, xid, k, role)
    w = F.World(detail=detail)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 0 and rep["ledger"]["detail_calls"] == 10
    assert rep["calls"][0]["status"] == "ok" and rep["q1_shape"]["v1_failures"] == {"item_not_dict": 1}


def test_analysis_exception_is_recorded_as_a_class_name(tmp_path, monkeypatch):
    real = K.aspect_eval
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ZeroDivisionError("boom")
        return real(*a, **k)
    monkeypatch.setattr(K, "aspect_eval", flaky)
    code, rep = F.run(F.World(), tmp=tmp_path)
    assert code == 0 and rep["calls"][0]["analysis_error"] == "ZeroDivisionError" and rep["ledger"]["detail_calls"] == 10
    assert rep["calls"][-1]["pool"] == "control"                    # 分析出错的对照不算有信息：F10 用对照后备


def test_missing_image_list_on_two_ok_posts_stops(tmp_path):
    def detail(xid, k, role):
        body = F.World.default_detail(w, xid, k, role)
        body["data"]["data"][0]["note_list"][0].pop("images_list", None)
        body["data"]["data"][0]["note_list"][0]["pics"] = []
        return body
    w = F.World(detail=detail)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 3 and rep["reason"] == "shape_absent" and rep["ledger"]["detail_calls"] == 2
    assert rep["verdicts"]["q1"] == "differs"


def test_unknown_shape_on_the_first_call_stops_after_one_send(tmp_path):
    w = F.World(detail=lambda xid, k, role: {"code": 200, "data": {"code": 0, "data": {"weird": 1}}})
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 3 and rep["reason"] == "unknown_shape_call1" and len(w.kinds("detail")) == 1


@pytest.mark.parametrize("resp,reason", [(P.Resp(401, {}, b"", ""), "auth"), (P.Resp(402, {}, b"", ""), "quota"),
                                         (P.Resp(429, {}, b"", ""), "rate_limited")])
def test_hard_stops(tmp_path, resp, reason):
    w = F.World(detail=lambda xid, k, role: resp if k == 2 else F.World.default_detail(w, xid, k, role))
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 3 and rep["reason"] == reason and len(w.kinds("detail")) == 2


# ── 20. plan / rehearse ──

def test_plan_reads_supabase_only(tmp_path, capsys):
    def never(*a, **k):
        raise AssertionError("plan must not call this")
    w = F.World(detail=never, image=never)
    code, rep = F.run(w, mode="plan", tmp=tmp_path)
    assert code == 0 and rep["status"] == "planned" and rep["verdicts"]["recommendation"] == "na"
    assert {x[0] for x in w.log} == {"supabase"}
    out = capsys.readouterr().out
    assert sum(1 for line in out.splitlines() if line.startswith("slot ")) == 10 and "without_feishu_image=310" in out
    assert "eligible=12" in out and "planned_calls=10" in out


def test_plan_needs_no_tikhub_key_or_token(tmp_path):
    w = F.World(env={"SUPABASE_URL": F.SUPA, "SUPABASE_SERVICE_ROLE_KEY": "k"})
    assert F.run(w, mode="plan", tmp=tmp_path)[0] == 0


def test_selection_failure_is_fail_closed(tmp_path):
    t = F.tables()
    t["v_notes_vanished"] = lambda url: P.Resp(404, {}, b"", "")
    w = F.World(t)
    code, rep = F.run(w, tmp=tmp_path)
    assert code == 2 and rep["reason"] == "selection_failed" and rep["selection_error"] == "http_404"
    assert {x[0] for x in w.log} == {"supabase"}
    w2 = F.World(env={})
    code2, rep2 = F.run(w2, tmp=tmp_path / "b")
    assert code2 == 2 and rep2["selection_error"] == "missing_env" and not w2.log


def test_raw_extra_fallback_when_json_paths_are_refused(tmp_path):
    t = F.tables()
    raw_rows = []
    for r in t["notes"]:
        ex = {"_undeclared": {"作品ID": r["wu"]} if r["wu"] else {}}
        if r["pt"]:
            ex["巡查状态"] = r["pt"]
        if r["at"] is not None:
            ex["已确认存活"] = r["at"]
        raw_rows.append({k: r[k] for k in ("note_id", "project_id", "platform", "tier", "publish_url")} | {"raw_extra": ex})
    seen = []

    def notes(url):
        seen.append(url)
        if "raw_extra-" in P.unquote(url):
            return P.Resp(400, {}, b"", "")
        q = parse_qs(urlsplit(url).query)
        off, lim = int(q["offset"][0]), int(q["limit"][0])
        assert lim == P.RAW_PAGE
        return P.Resp(200, {}, json.dumps(raw_rows[off:off + lim], ensure_ascii=False).encode(), "")
    t["notes"] = notes
    w = F.World(t)
    code, rep = F.run(w, mode="plan", tmp=tmp_path)
    assert code == 0 and rep["selection"]["source"] == "raw_extra_fallback" and len(seen) == 3
    assert rep["selection"]["controls"]["eligible"] == 12 and rep["selection"]["without_feishu_image"] == 310


def test_supabase_requests_are_get_only_and_encode_chinese(tmp_path):
    w = F.World()
    F.run(w, mode="plan", tmp=tmp_path)
    for _, method, data, url in w.kinds("supabase"):
        assert method == "GET" and data is None and url.startswith(F.SUPA + "/rest/v1/")
        assert all(ord(ch) < 128 for ch in url)
    paths = [P.unquote(x[3]) for x in w.kinds("supabase")]
    assert any("raw_extra->_undeclared->>作品ID" in p for p in paths)
    assert not any("first_file" in p for p in paths)


def test_rehearse_runs_offline_without_any_env(tmp_path, monkeypatch):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "TIKHUB_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    def no_network(*a, **k):
        raise AssertionError("rehearse must not use the real transport")
    monkeypatch.setattr(P, "http_fetch", no_network)
    code = P.main(["rehearse", "--out", str(tmp_path / "out"), "--recheck-minutes", "10"])
    rep = json.loads((tmp_path / "out" / P.REPORT_JSON).read_text(encoding="utf-8"))
    assert code == 0 and rep["mode"] == "rehearse" and rep["selection"]["source"] == "rehearse_fake"
    assert rep["ledger"]["detail_calls"] == 10 and rep["scrubbed"] == 0
    assert rep["verdicts"]["recommendation"] == "ask_owner_full_run"
