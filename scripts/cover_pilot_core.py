# -*- coding: utf-8 -*-
"""I-35 封面试点的纯逻辑：选样、记账、状态词、形状记录、比对、下载判读、结论规则、报告清洗。

不联网、不读环境变量、不看时钟；I/O 和命令行在 scripts/cover_pilot.py，说明在 docs/cover-pilot-2026-11-18.md。
本仓和 Actions 日志、产物都是公开的：这里产出的一切只许是计数、固定状态词、键名、形状串和宽高比统计。
笔记 id、链接、标题、昵称、项目代号、file token、返回体、cache_url、余额（只许差值）、密钥一概不出这个进程。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import struct
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge import external as E  # noqa: E402

SCHEMA = "cover-pilot-i35-v1"
SEED = "i35-cover-pilot-1"

# ── 钱（owner 11-18 同意的是「约 0.7 元试 10 篇」）：写死在这里，命令行和 workflow 都改不了 ──
MAX_CALLS = 10
MAX_YUAN = 0.75
USD_PER_CALL = 0.01
FX = 7.2
PRICE_YUAN = 0.072                  # = USD_PER_CALL × FX，linkcheck providers.py 同一口径
MAX_BALANCE_READS = 2               # 零费用的余额端点：前后各一次
MAX_IMAGE_REQUESTS = 40             # 小红书图片服务器（不经 TikHub、不计费）；含 https→http 退路和最多 2 跳同域跳转
BALANCE_FLOOR_USD = 0.50            # 共用账户低于这个数就不跑：给 linkcheck 的巡查留余量

ASPECT_TOL = 0.01                   # |ln(宽高比之比)|：飞书缩图取整最多挪约 0.4%
PLACEHOLDER_TOL = 0.05              # 下到的图和 JSON 里的宽高比差这么多以上 → 疑似防盗链占位图
TALL = 1.40                         # 长边 / 短边 > 1.40：比 3:4 更长（能把列表里的项分开）
RATIO_3_4_TOL = 0.01
EXPECTED_WITHOUT_IMAGE = 310        # 11-18 计数：427 篇爆帖里 310 篇没有飞书首图
DRIFT = 30
MIN_CONTROLS = 4
MIN_TARGETS = 3
RECHECK_CHOICES = (0, 10, 20, 30)
RECHECK_BUDGET_MIN = 45             # 已用时间 + 复查等待超过这个数就不复查（job 上限 60 分钟）
CLAIM_TAG = "cover-pilot-i35-run-claimed"
HIT_TIERS = ("爆", "大爆")
PATROL_OK = "正常"
# external-corpus 的定时（周一、周四 03:07 UTC）在同一个 TikHub 账户上花钱：这两天 02:00–13:00 UTC 不跑（余额差值会被它污染）
CRAWL_DAYS = (0, 3)
CRAWL_HOURS = (2, 13)

STATUS_WORDS = ("ok", "gone", "hidden", "upstream_failed", "bad_request", "not_found", "server", "network",
                "unknown_shape", "auth", "quota", "rate_limited")
FAIL_WORDS = frozenset({"upstream_failed", "bad_request", "not_found", "server", "network", "unknown_shape"})
HARD_STOP = frozenset({"auth", "quota", "rate_limited"})
NEUTRAL = frozenset({"gone", "hidden"})
IMAGE_WORDS = ("ok", "forbidden", "not_found", "redirect", "client_error", "server", "network", "too_large", "not_image",
               "placeholder_suspect", "url_rejected", "no_url", "not_attempted", "download_error")
DETERMINISTIC_REFUSALS = frozenset({"forbidden", "not_found", "not_image", "placeholder_suspect", "redirect", "client_error"})
TIERS = ("bare", "browser_ua", "browser_ua_referer")
# linkcheck xhsearch/transport.py BROWSER_UA 原样
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
XHS_REFERER = "https://www.xiaohongshu.com/"
IMAGE_ACCEPT = "image/webp,image/jpeg,image/*;q=0.8"
URL_KEYS = ("url_size_large", "url", "original")        # 只取这三个；live_photo / video* / url_multi_level 一律不碰
VIDEO_KEYS = ("video_info_v2", "video_info", "video")
IMAGES_PATH = "data.data[0].note_list[0].images_list"

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
COVER_RE = re.compile(r"(cover|thumb|first_frame|poster|image|img|pic)")
XHS_NAMED_RE = re.compile(r"^(xhs|xiaohongshu|rednote|redbook)[a-z0-9-]*\.")
KNOWN_XHS_DOMAINS = ("xhscdn.com", "xiaohongshu.com")
EXPIRY_NAMES = frozenset({"sign", "signature", "t", "ts", "time", "timestamp", "e", "exp", "expire", "expires", "x-expires",
                          "deadline", "auth_key", "token", "x-amz-expires", "x-amz-signature", "x-oss-expires",
                          "x-oss-signature"})
LONG_RE = re.compile(r"xiaohongshu\.com/(explore|discovery/item)/([0-9a-fA-F]{24})(?![0-9A-Za-z])", re.I)
CELL_RE = re.compile(r"^\s*([0-9a-f]{24}|\d{6,})\s*←\s*(\S+)\s*$", re.I)
XID_RE = re.compile(r"^[0-9a-f]{24}$")


class CapReached(Exception):
    """再发一次就超上限：这一次不发。"""


class SelectionFailed(Exception):
    """读 TV 失败：一分钱都没花就停（fail closed）。args[0] 只是一个状态词（http_400 / 类名），不带 URL。"""


# ════════════════════════════════════════════════════════════════════
# 选样
# ════════════════════════════════════════════════════════════════════

def rank(tv_note_id: str) -> str:
    """和 TV scripts/first_image.py 的 _rank 同一个构造，换了种子。"""
    return hashlib.md5((str(tv_note_id) + SEED).encode("utf-8")).hexdigest()


def cell_text(v: Any, depth: int = 0) -> str:
    """飞书单元格 → 文本：字符串原样；富文本段 [{"text": …}] 拼起来；PostgREST ->> 给的 JSON 文本先解开。"""
    if depth > 4 or v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("[", "{"):
            try:
                return cell_text(json.loads(s), depth + 1)
            except ValueError:
                return s
        return s
    if isinstance(v, list):
        return "".join(cell_text(x, depth + 1) for x in v).strip()
    if isinstance(v, dict):
        return cell_text(v.get("text") if v.get("text") is not None else v.get("link"), depth + 1)
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def id_from_long_link(url: Any) -> tuple:
    """长链接里的 24 位十六进制 id → (id, long_explore | long_discovery)；没有 → (None, None)。25 位的不认。"""
    if not isinstance(url, str):
        return None, None
    m = LONG_RE.search(url)
    if not m:
        return None, None
    return m.group(2).lower(), ("long_discovery" if m.group(1).lower().startswith("discovery") else "long_explore")


def id_from_cell(cell: Any, publish_url: Any) -> Optional[str]:
    """linkcheck 的「作品ID」格：「<24 位 id> ← <从哪个链接解出来的>」。右边去掉末尾 / 要等于现在的发布链接，否则是旧的、不认。"""
    m = CELL_RE.match(cell_text(cell))
    if not m:
        return None
    nid = m.group(1).lower()
    pu = (publish_url or "").strip().rstrip("/") if isinstance(publish_url, str) else ""
    if not XID_RE.match(nid) or not pu or m.group(2).rstrip("/") != pu:
        return None
    return nid


def patrol_of(v: Any) -> Optional[str]:
    t = cell_text(v)
    return t or None


def alive_of(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def first_valid(values: Iterable, fn: Callable) -> Any:
    """顶层和 _undeclared 两处都试，取第一个解得出来的（顶层有个空值或不像 id 的值时不挡住 _undeclared 里那个好的）。"""
    for v in values:
        r = fn(v)
        if r is not None:
            return r
    return None


def _pos_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float) and v.is_integer():
        return int(v) if v > 0 else None
    if isinstance(v, str) and v.strip().isdigit():
        n = int(v.strip())
        return n if n > 0 else None
    return None


def _strata(w: int, h: int) -> tuple:
    tall = max(w, h) / min(w, h) > TALL
    r34 = abs(math.log((h / w) / (4 / 3))) <= RATIO_3_4_TOL
    return tall, r34


def build_pools(notes: list, source: dict, fi: dict, excluded: set, vanished: set) -> tuple:
    """notes：read_tv 归一化后的行；source：note_id → {n_files, file_sha}（v_note_first_image_source）；
    fi：note_id → note_first_image 行；excluded：先不做的项目；vanished：飞书里已删的篇。
    返回 (pools, funnel)。pools 里的候选带 TV note_id 和 XHS id，只在内存里用，绝不进报告。"""
    f = {"hits": 0, "hits_xhs": 0, "with_feishu_image": 0, "without_feishu_image": 0, "without_feishu_image_xhs": 0}
    t: Counter = Counter()
    c: Counter = Counter()
    targets, controls = [], []
    for n in notes:
        if n.get("tier") not in HIT_TIERS:
            continue
        f["hits"] += 1
        nid = n.get("note_id")
        xhs = n.get("platform") == "xiaohongshu"
        has_img = nid in source
        f["hits_xhs"] += int(xhs)
        if has_img:
            f["with_feishu_image"] += 1
        else:
            f["without_feishu_image"] += 1
            f["without_feishu_image_xhs"] += int(xhs)
        if not xhs:
            continue
        url = n.get("publish_url") if isinstance(n.get("publish_url"), str) else ""
        long_id, kind = id_from_long_link(url)
        cell_id = first_valid((n.get("wid_top"), n.get("wid_und")), lambda v: id_from_cell(v, url))
        conflict = bool(long_id and cell_id and long_id != cell_id)
        xid = None if conflict else (long_id or cell_id)
        link = kind if long_id else ("short_with_id" if cell_id else "short_no_id")
        base = {"tv": nid, "xid": xid, "rank": rank(nid), "link": link}
        if not has_img:
            t[link] += 1
            if nid in vanished:
                t["vanished"] += 1
                if link == "short_with_id":
                    t["short_with_id_vanished"] += 1
                continue
            if conflict:
                t["id_conflict"] += 1
                continue
            if not xid:
                continue                        # short_no_id：展开短链是一次小红书请求，这里不做
            targets.append(dict(base, pool="target", stratum=link))
            continue
        c["in_source"] += 1
        row, src = fi.get(nid), source[nid]
        if n.get("project_id") in excluded:
            c["excluded_project"] += 1
            continue
        if nid in vanished:
            c["vanished"] += 1
            continue
        if row is None:
            c["no_fi_row"] += 1
            continue
        if row.get("file_sha") != src.get("file_sha"):
            c["sha_changed"] += 1
            continue
        if row.get("status") != "answered":
            c["not_answered"] += 1
            if row.get("status") == "not_a_cover":
                c["not_a_cover_seen"] += 1
            continue
        w, h = _pos_int(row.get("width")), _pos_int(row.get("height"))
        if not (w and h):
            c["no_dims"] += 1
            continue
        patrol = first_valid((n.get("patrol_top"), n.get("patrol_und")), patrol_of)
        alive = first_valid((n.get("alive_top"), n.get("alive_und")), alive_of)
        if patrol != PATROL_OK or alive is not True:
            c["not_alive"] += 1
            continue
        if conflict:
            c["id_conflict"] += 1
            continue
        if not xid:
            c["no_id"] += 1
            continue
        single = _pos_int(src.get("n_files")) == 1
        tall, r34 = _strata(w, h)
        shape = "tall" if tall else ("ratio_3_4" if r34 else "other")
        controls.append(dict(base, pool="control", stratum=f"{'single' if single else 'multi'}_{shape}", single=single,
                             tall=tall, ratio_3_4=r34, fs_w=w, fs_h=h, fs_bytes=_pos_int(row.get("file_bytes")),
                             n_files=_pos_int(src.get("n_files")) or 0))
    seen = Counter(x["xid"] for x in targets + controls)
    for x in targets + controls:
        if seen[x["xid"]] > 1:
            (t if x["pool"] == "target" else c)["id_dup"] += 1
    targets = [x for x in targets if seen[x["xid"]] == 1]
    controls = [x for x in controls if seen[x["xid"]] == 1]
    t["eligible"] = len(targets)
    c["eligible"] = len(controls)
    c["single"] = sum(1 for x in controls if x["single"])
    c["multi"] = len(controls) - c["single"]
    c["tall"] = sum(1 for x in controls if x["tall"])
    c["ratio_3_4"] = sum(1 for x in controls if x["ratio_3_4"])

    def by_rank(xs):
        return sorted(xs, key=lambda x: x["rank"])

    def alternate(a, b):
        out = []
        for i in range(max(len(a), len(b))):
            out += a[i:i + 1] + b[i:i + 1]
        return out

    # 高 / 宽图和非高图交替（高图先）：只有比 3:4 更长的图才分得开列表里的项；但小红书要是把长图裁成 3:4，全押高图会全落空
    singles = [x for x in controls if x["single"]]
    multis = [x for x in controls if not x["single"]]
    pools = {"CS": alternate(by_rank([x for x in singles if x["tall"]]), by_rank([x for x in singles if not x["tall"]])),
             "CM": alternate(by_rank([x for x in multis if x["tall"]]), by_rank([x for x in multis if not x["tall"]])),
             "TL": by_rank([x for x in targets if x["link"] in ("long_explore", "long_discovery")]),
             "TS": by_rank([x for x in targets if x["link"] == "short_with_id"])}
    tkeys = ("long_explore", "long_discovery", "short_with_id", "short_with_id_vanished", "short_no_id", "vanished",
             "id_conflict", "id_dup", "eligible")
    ckeys = ("in_source", "excluded_project", "vanished", "no_fi_row", "sha_changed", "not_answered", "not_a_cover_seen",
             "no_dims", "not_alive", "id_conflict", "no_id", "id_dup", "eligible", "single", "multi", "tall", "ratio_3_4")
    funnel = dict(f, targets={k: int(t.get(k, 0)) for k in tkeys}, controls={k: int(c.get(k, 0)) for k in ckeys})
    return pools, funnel


def drift_word(funnel: dict) -> str:
    return "ok" if abs(funnel["without_feishu_image"] - EXPECTED_WITHOUT_IMAGE) <= DRIFT else "drift"


def in_crawl_window(now) -> bool:
    """now 是带时区的 UTC 时刻。周一、周四 02:00–12:59 UTC：external-corpus 在同一个 TikHub 账户上花钱的时段。"""
    return now.weekday() in CRAWL_DAYS and CRAWL_HOURS[0] <= now.hour < CRAWL_HOURS[1]


CALL_ORDER = ("C1", "C2", "T1", "C3", "T2", "C4", "T3", "C5", "C6", "F10")


def plan_slots(pools: dict) -> dict:
    """10 个槽位，全在 10 次上限之内：C1–C4 单图对照（CS 不够由 CM 补），C5–C6 多图对照，T1–T3 目标，F10 机动。
    顺序 C1, C2, T1, C3, T2, C4, T3, C5, C6, F10：对照打头（最可能还在，先验明外壳形状），目标不相邻（两篇删了的不会连成「连续失败」）。"""
    cs, cm = list(pools["CS"]), list(pools["CM"])
    controls = cs[:4]
    rest_cs = cs[4:]
    need = 4 - len(controls)
    controls += cm[:need]
    cm = cm[need:]
    controls += cm[:2]
    cm = cm[2:]
    tl, ts = list(pools["TL"]), list(pools["TS"])
    t1 = tl.pop(0) if tl else None
    t2 = ts.pop(0) if ts else (tl.pop(0) if tl else None)
    t3 = tl.pop(0) if tl else (ts.pop(0) if ts else None)
    cc = controls + [None] * (6 - len(controls))
    slots = {"C1": cc[0], "C2": cc[1], "C3": cc[2], "C4": cc[3], "C5": cc[4], "C6": cc[5], "T1": t1, "T2": t2, "T3": t3}
    return {"slots": slots, "control_reserves": rest_cs + cm, "target_reserves": tl + ts}


def control_informative(rec: dict) -> bool:
    return (rec.get("status") == "ok" and not rec.get("analysis_error") and rec.get("path") != "absent"
            and (rec.get("aspect") or {}).get("category") not in (None, "no_dims"))


def flex_choice(control_recs: list, plan: dict) -> Optional[dict]:
    """F10：C1–C6 里有一篇不是 ok（或 ok 但没宽高 / 没图片列表 / 分析出错），用第一个对照后备；否则下一个目标。都没有就不发。"""
    if any(not control_informative(r) for r in control_recs) and plan["control_reserves"]:
        return plan["control_reserves"][0]
    if plan["target_reserves"]:
        return plan["target_reserves"][0]
    return None


def slot_label(slot: str, cand: Optional[dict]) -> str:
    if slot == "F10":
        return "F10 flex control_reserve_or_target"
    if cand is None:
        return f"{slot} empty"
    return f"{slot} {cand['pool']} {cand['stratum']}"


# ════════════════════════════════════════════════════════════════════
# 记账
# ════════════════════════════════════════════════════════════════════

class Ledger:
    """每次发之前查；查过了先记账再发。发出去失败的照样算钱，不重试、不退。"""

    def __init__(self):
        self.detail_calls = 0
        self.balance_reads = 0
        self.image_requests = 0
        self._budget = E.Budget(limit_usd=0.10, price_per_call=0.01)    # 第二道、独立的上限：Jev 自己的 Budget

    @property
    def yuan(self) -> float:
        return round(self.detail_calls * PRICE_YUAN, 4)

    def check_detail(self) -> None:
        n = self.detail_calls + 1
        if n > MAX_CALLS:
            raise CapReached("calls")
        if round(n * PRICE_YUAN, 4) > MAX_YUAN + 1e-9:
            raise CapReached("yuan")
        if (self._budget.calls + 1) * self._budget.price_per_call > self._budget.limit_usd + 1e-9:
            raise CapReached("budget")

    def charge_detail(self) -> None:
        self.check_detail()
        try:
            self._budget.charge(1)
        except E.BudgetExceeded:
            raise CapReached("budget") from None
        self.detail_calls += 1

    def charge_balance_read(self) -> None:
        if self.balance_reads + 1 > MAX_BALANCE_READS:
            raise CapReached("balance_reads")
        self.balance_reads += 1

    def charge_image(self) -> None:
        if self.image_requests + 1 > MAX_IMAGE_REQUESTS:
            raise CapReached("image_requests")
        self.image_requests += 1


# ════════════════════════════════════════════════════════════════════
# 详情返回 → 状态词
# ════════════════════════════════════════════════════════════════════

def _code_word(code: Optional[int]) -> Optional[str]:
    if code in (401, 403):
        return "auth"
    if code == 402:
        return "quota"
    if code == 429:
        return "rate_limited"
    if code in (400, 422):
        return "bad_request"
    if code == 404:
        return "not_found"
    if code is not None and 500 <= code < 600:
        return "server"
    return None


def http_word(status: int) -> str:
    w = _code_word(status)
    if w:
        return w
    if 400 <= status < 500:
        return "bad_request"
    return "unknown_shape"


def classify_raw(raw: dict) -> str:
    code = raw.get("code")
    if code is not None and E._as_int(code) != 200:
        return _code_word(E._as_int(code)) or "upstream_failed"
    if raw.get("success") is False:
        return "upstream_failed"
    data = raw.get("data")
    if not isinstance(data, dict):
        return "unknown_shape"
    if E._search_business_error(raw):
        return "upstream_failed"                 # 外壳 200 包着上游失败：照样计费，不能当成「删了」
    inner = data.get("data")
    if not isinstance(inner, list):
        return "unknown_shape"
    if not inner:
        return "gone"
    d0 = inner[0]
    if not isinstance(d0, dict) or not isinstance(d0.get("note_list"), list):
        return "unknown_shape"
    if not d0["note_list"]:
        return "gone"
    n0 = d0["note_list"][0]
    if not isinstance(n0, dict):
        return "unknown_shape"
    if n0.get("model_type") == "error":
        return "hidden"                          # 只有作者看得见（linkcheck 09-14 实测）；它自带的 image 键是静态占位图，不碰
    return "ok"


def classify(status: int, body: Optional[bytes]) -> tuple:
    """(状态词, 解析后的 dict 或 None)。"""
    if status != 200:
        return http_word(status), None
    try:
        raw = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "unknown_shape", None
    if not isinstance(raw, dict):
        return "unknown_shape", None
    return classify_raw(raw), raw


def note_of(raw: dict) -> dict:
    return raw["data"]["data"][0]["note_list"][0]


class StopRule:
    """auth / quota / rate_limited 立刻停；第 1 次就认不出形状停；连续 2 次失败停（gone / hidden 不算也不清零，ok 清零）；
    2 篇 ok 但不是视频、也没有 images_list 停（形状变了，再花钱也答不了 Q2、Q3）。"""

    def __init__(self):
        self.n = 0
        self.streak = 0
        self.shape_absent = 0

    def after(self, word: str, path_absent_nonvideo: bool = False) -> Optional[str]:
        self.n += 1
        if word in HARD_STOP:
            return word
        if word == "unknown_shape" and self.n == 1:
            return "unknown_shape_call1"
        if word in FAIL_WORDS:
            self.streak += 1
        elif word == "ok":
            self.streak = 0
        if self.streak >= 2:
            return "consecutive_failures"
        if word == "ok" and path_absent_nonvideo:
            self.shape_absent += 1
            if self.shape_absent >= 2:
                return "shape_absent"
        return None


# ════════════════════════════════════════════════════════════════════
# 形状（Q1）：只记键名和值的类型
# ════════════════════════════════════════════════════════════════════

def safe_key(k: Any) -> bool:
    return (isinstance(k, str) and bool(KEY_RE.match(k)) and not re.fullmatch(r"[0-9a-f]{8,}", k)
            and not re.search(r"\d{6,}", k) and not re.search(r"[0-9a-f]{12,}", k))


def safe_param(k: Any) -> bool:
    return (isinstance(k, str) and bool(PARAM_RE.match(k)) and not re.fullmatch(r"[0-9a-f]{8,}", k)
            and not re.search(r"\d{6,}", k) and not re.search(r"[0-9a-f]{12,}", k))


def type_word(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str_url" if v.lower().startswith(("http://", "https://")) else "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "other"


def is_video(note: dict) -> bool:
    return note.get("type") == "video" or any(k in note for k in VIDEO_KEYS)


def word_in(v: Any, allowed: tuple) -> str:
    if v is None:
        return "absent"
    return v if isinstance(v, str) and v in allowed else "other"


def choose_url(item: Any) -> tuple:
    """item 0 的图片链接：url_size_large → url → original 里第一个 http(s) 字符串。(链接, 键名) 或 (None, None)。"""
    if not isinstance(item, dict):
        return None, None
    for k in URL_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.strip().lower().startswith(("https://", "http://")):
            return v.strip(), k
    return None, None


class Shape:
    """各篇 ok 的返回累加：信封 / data / data[0] / note / 图片项的键名计数，item 0 的值类型，像封面的键路径。"""

    CAPS = {"envelope": 25, "data": 25, "data0": 25, "note": 80}

    def __init__(self):
        self.keys = {k: Counter() for k in self.CAPS}
        self.item_keys: Counter = Counter()
        self.item0_types: dict = {}
        self.item0_subkeys: dict = {}
        self.cover_like: dict = {}
        self.video_cover: Counter = Counter()
        self.path: Counter = Counter()
        self.v1_fail: Counter = Counter()
        self.odd = 0
        self.truncated = 0

    def _names(self, d: Any, cap: int) -> list:
        out = []
        if not isinstance(d, dict):
            return out
        for k in d:
            if not safe_key(k):
                self.odd += 1
            elif len(out) >= cap:
                self.truncated += 1
            else:
                out.append(k)
        return out

    def _walk_cover(self, d: Any, prefix: tuple, depth: int, found: dict) -> None:
        if depth > 3 or not isinstance(d, dict):
            return
        for k, v in d.items():
            if not safe_key(k):
                continue
            path = prefix + (k,)
            if COVER_RE.search(k) and len(found) < 20:
                found.setdefault(".".join(path), type_word(v))
            if isinstance(v, dict):
                self._walk_cover(v, path, depth + 1, found)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                self._walk_cover(v[0], path, depth + 1, found)

    def add(self, raw: dict, note: dict) -> dict:
        """一篇 ok 的返回。信封只记顶层键名（cache_url / params / router 只作为名字出现，不往里走）。返回这篇的 Q1 判读。"""
        for name, obj in (("envelope", raw), ("data", raw.get("data")), ("data0", raw["data"]["data"][0]), ("note", note)):
            self.keys[name].update(self._names(obj, self.CAPS[name]))
        video = is_video(note)
        found: dict = {}
        self._walk_cover(note, (), 1, found)
        for p, tw in found.items():
            self.cover_like.setdefault(p, Counter())[tw] += 1
            if video:
                self.video_cover[p] += 1
        images = note.get("images_list")
        path = IMAGES_PATH if isinstance(images, list) else "absent"
        self.path[path] += 1
        fail = None
        if not isinstance(images, list):
            fail = "images_list_absent"
        elif not images:
            fail = "images_list_empty"
        elif not all(isinstance(it, dict) for it in images):
            fail = "item_not_dict"
        if isinstance(images, list):
            for it in images[:50]:
                if isinstance(it, dict):
                    for k in it:
                        if not safe_key(k):
                            self.odd += 1
                        elif k in self.item_keys or len(self.item_keys) < 40:
                            self.item_keys[k] += 1
                        else:
                            self.truncated += 1
        if fail is None:
            item0 = images[0]
            for k, v in item0.items():
                if not safe_key(k):
                    continue
                self.item0_types.setdefault(k, Counter())[type_word(v)] += 1
                if isinstance(v, dict):
                    sub = self.item0_subkeys.setdefault(k, Counter())
                    sub.update(self._names(v, 12))
            for k in ("index", "width", "height"):
                if k not in item0:
                    fail = f"no_{k}"
                    break
            if fail is None and choose_url(item0)[0] is None:
                fail = "no_url"
        if fail:
            self.v1_fail[fail] += 1
        return {"path": path, "v1_ok": fail is None, "v1_fail": fail, "is_video": video,
                "note_type": word_in(note.get("type"), ("normal", "video", "multi")),
                "model_type": word_in(note.get("model_type"), ("note", "normal", "video", "error"))}


# ════════════════════════════════════════════════════════════════════
# 顺序与宽高比（Q2）
# ════════════════════════════════════════════════════════════════════

def _idx(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def index_order(images: list) -> str:
    n = len(images)
    if n == 0:
        return "na"
    if n == 1:
        return "single"
    if any(not isinstance(it, dict) or "index" not in it for it in images):
        return "missing"
    idx = [_idx(it["index"]) for it in images]
    if any(i is None for i in idx):
        return "non_int"
    if idx == list(range(n)):
        return "sequential_0"
    if idx == list(range(1, n + 1)):
        return "sequential_1"
    if any(b < a for a, b in zip(idx, idx[1:])):
        return "unsorted"                               # 后面有一项比前面小: 和显示顺序对不上
    if all(a < b for a, b in zip(idx, idx[1:])):
        return "sorted_gaps"                            # 递增、中间缺号: 第 0 项还是最小的, 只是说明不了什么
    return "ties"                                       # 有相同的号 (比如全是 0): 这个字段不带顺序信息


def item_aspect(item: Any) -> tuple:
    """(高/宽, 来源 item | origin_img | none)。"""
    if not isinstance(item, dict):
        return None, "none"
    w, h = _pos_int(item.get("width")), _pos_int(item.get("height"))
    if w and h:
        return h / w, "item"
    o = item.get("origin_img")
    if isinstance(o, dict):
        w, h = _pos_int(o.get("width")), _pos_int(o.get("height"))
        if w and h:
            return h / w, "origin_img"
    return None, "none"


def item_dims(item: Any) -> Optional[tuple]:
    if not isinstance(item, dict):
        return None
    for d in (item, item.get("origin_img") if isinstance(item.get("origin_img"), dict) else {}):
        w, h = _pos_int(d.get("width")), _pos_int(d.get("height"))
        if w and h:
            return w, h
    return None


def _ldist(a: float, b: float) -> float:
    return abs(math.log(a / b))


def _index_word(i: int) -> str:
    return str(i) if i < 3 else "3_plus"


def aspect_eval(images: list, fs_w: Optional[int], fs_h: Optional[int]) -> dict:
    """对照篇：每一项的宽高比和飞书首图（缩过的，宽高比不变，EXIF 已转正）比。只看宽高比，不看像素：证据弱。"""
    out = {"category": "no_dims", "d0": None, "d_other_min": None, "best_index": "none", "dims_source": "none",
           "origin_vs_item": "na", "discriminating": False}
    fw, fh = _pos_int(fs_w), _pos_int(fs_h)
    if not images or not (fw and fh):
        return out
    r_fs = fh / fw
    rs = [item_aspect(it) for it in images]
    out["dims_source"] = rs[0][1]
    it0 = images[0] if isinstance(images[0], dict) else {}
    o = it0.get("origin_img") if isinstance(it0.get("origin_img"), dict) else {}
    a_item = (_pos_int(it0.get("width")), _pos_int(it0.get("height")))
    a_orig = (_pos_int(o.get("width")), _pos_int(o.get("height")))
    if all(a_item) and all(a_orig):
        out["origin_vs_item"] = ("match" if _ldist(a_item[1] / a_item[0], a_orig[1] / a_orig[0]) <= ASPECT_TOL
                                 else "mismatch")
    if rs[0][0] is None:
        return out
    d = [None if r is None else _ldist(r, r_fs) for r, _ in rs]
    out["d0"] = round(d[0], 4)
    others = [x for x in d[1:] if x is not None]
    out["d_other_min"] = round(min(others), 4) if others else None
    out["discriminating"] = any(r is not None and _ldist(r, rs[0][0]) > ASPECT_TOL for r, _ in rs[1:])
    cands = [(x, i) for i, x in enumerate(d) if x is not None]
    best = min(cands)                                   # 并列时 index 小的在前（0 优先）
    out["best_index"] = _index_word(best[1]) if best[0] <= ASPECT_TOL else "none"
    if len(images) == 1:
        out["category"] = "single_image_match" if d[0] <= ASPECT_TOL else "single_image_no_match"
    elif d[0] <= ASPECT_TOL:
        out["category"] = "item0_shared_match" if any(x <= ASPECT_TOL for x in others) else "item0_unique_match"
    else:
        out["category"] = "other_match_only" if any(x <= ASPECT_TOL for x in others) else "no_match"
    return out


def size_match_index(images: list, fs_bytes: Optional[int]) -> str:
    """探索性：飞书附件的字节数等于哪一项的 origin_img.size（origin_img.size 的含义没核实过，不进结论）。"""
    fb = _pos_int(fs_bytes)
    if not fb:
        return "no_size"
    sizes = []
    for it in images:
        o = it.get("origin_img") if isinstance(it, dict) and isinstance(it.get("origin_img"), dict) else {}
        sizes.append(_pos_int(o.get("size")))
    if not any(sizes):
        return "no_size"
    for i, s in enumerate(sizes):
        if s == fb:
            return _index_word(i)
    return "none"


def n_files_cmp(n_images: int, n_files: int) -> str:
    if not n_files:
        return "na"
    return "xhs_more" if n_images > n_files else ("equal" if n_images == n_files else "xhs_fewer")


def n_images_bucket(n: Optional[int]) -> str:
    if n is None:
        return "na"
    return "0" if n == 0 else "1" if n == 1 else "2_3" if n <= 3 else "4_6" if n <= 6 else "7_plus"


def d_bucket(d: Optional[float]) -> str:
    if d is None:
        return "na"
    return "le_tol" if d <= ASPECT_TOL else "tol_to_0_05" if d <= 0.05 else "0_05_to_0_2" if d <= 0.2 else "gt_0_2"


# ════════════════════════════════════════════════════════════════════
# 下载（Q3）：链接闸、字节判读、链接形状
# ════════════════════════════════════════════════════════════════════

_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*")
_LABEL = re.compile(r"^[a-z0-9-]{1,63}$")


def url_gate(url: Any) -> tuple:
    """(过不过, 原因词)。只认 http / https、没有账号密码、默认端口、像样的域名（不是 IP、不是 localhost）。"""
    if not isinstance(url, str):
        return False, "scheme"
    try:
        p = urlsplit(url)
    except ValueError:
        return False, "host"
    scheme = p.scheme.lower()
    if scheme not in ("https", "http"):
        return False, "scheme"
    if "@" in p.netloc or p.username is not None or p.password is not None:
        return False, "userinfo"
    try:
        port = p.port
    except ValueError:
        return False, "port"
    if port not in (None, 443 if scheme == "https" else 80):
        return False, "port"
    if "[" in p.netloc:
        return False, "ip"
    host = (p.hostname or "").lower()
    if not host:
        return False, "host"
    try:
        ipaddress.ip_address(host)
        return False, "ip"
    except ValueError:
        pass
    if _NUMERIC_HOST.fullmatch(host):
        return False, "ip"
    if host == "localhost" or host.endswith(".localhost"):
        return False, "host"
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(x) for x in labels) or not re.fullmatch(r"[a-z]{2,63}", labels[-1]):
        return False, "host"
    return True, ""


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable(host: str) -> str:
    labels = host.lower().split(".")
    if len(labels) >= 3 and labels[-2] in ("com", "net", "org", "gov", "edu") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def xhs_named(host: str) -> bool:
    return bool(XHS_NAMED_RE.match(registrable(host)))


def https_variant(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit(("https", (p.hostname or "").lower(), p.path, p.query, p.fragment))


def value_shape(v: str) -> str:
    n = len(v)
    lb = "1_8" if n <= 8 else "9_16" if n <= 16 else "17_32" if n <= 32 else "33_plus"
    if not v:
        return "empty"
    if v.isdigit():
        cls = "digits"
    elif re.fullmatch(r"[0-9a-fA-F]+", v):
        cls = "hex"
    elif re.fullmatch(r"[A-Za-z0-9]+", v):
        cls = "alnum"
    elif re.fullmatch(r"[A-Za-z0-9+/=_-]+", v):
        cls = "b64ish"
    else:
        cls = "other"
    return f"{cls}_{lb}"


def url_shape(url: str) -> dict:
    """查询参数只记名字（过名字闸的），过期类参数再记一个值的形状类（数字 / 十六进制…+ 长度档）；不记值、主机、路径。"""
    p = urlsplit(url)
    names, odd, shapes = [], 0, {}
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if safe_param(k):
            names.append(k)
            if k in EXPIRY_NAMES:
                shapes[k] = value_shape(v)
        else:
            odd += 1
    return {"scheme": p.scheme.lower(), "param_names": names, "odd_params": odd, "expiry_shapes": shapes,
            "expiry_like": any(k in EXPIRY_NAMES for k in names),
            "path_segments": len([s for s in p.path.split("/") if s])}


PNG_SIG = b"\x89PNG\r\n\x1a\n"
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def sniff(b: bytes) -> str:
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if b[:8] == PNG_SIG:
        return "png"
    if b[:4] == b"GIF8":
        return "gif"
    if len(b) >= 12 and b[4:8] == b"ftyp":
        brand = b[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
    return "other"


def dims_from_bytes(b: bytes) -> Optional[tuple]:
    """(宽, 高)；纯标准库读 PNG IHDR、GIF、WEBP（VP8 / VP8L / VP8X）、JPEG SOFn。读不出 → None。"""
    try:
        if b[:8] == PNG_SIG and b[12:16] == b"IHDR":
            return struct.unpack(">II", b[16:24])
        if b[:4] == b"GIF8":
            return struct.unpack("<HH", b[6:10])
        if len(b) >= 30 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            chunk = b[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
            if chunk == b"VP8 " and b[23:26] == b"\x9d\x01\x2a":
                return struct.unpack("<H", b[26:28])[0] & 0x3FFF, struct.unpack("<H", b[28:30])[0] & 0x3FFF
            if chunk == b"VP8L" and b[20] == 0x2F:
                bits = int.from_bytes(b[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            return None
        if b[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 <= len(b):
                if b[i] != 0xFF:
                    return None
                m = b[i + 1]
                if m == 0xFF:
                    i += 1
                    continue
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", b[i + 2:i + 4])[0]
                if m in _SOF:
                    h, w = struct.unpack(">HH", b[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError):
        return None
    return None


def bytes_vs_json(b: bytes, dims: Optional[tuple]) -> str:
    """下到的图的宽高比和 JSON 里这一项的比：match / mismatch / unparsed（读不出尺寸）/ na（JSON 里没有）。
    转了 90° 的也算 match（JPEG 的 EXIF 方向不转 SOF 里的宽高）。"""
    if not dims:
        return "na"
    got = dims_from_bytes(b)
    if not got or not got[0] or not got[1]:
        return "unparsed"
    r = got[1] / got[0]
    want = dims[1] / dims[0]
    d = min(_ldist(r, want), _ldist(1 / r, want))
    return "match" if d <= PLACEHOLDER_TOL else "mismatch"


def ct_family(ct: Any) -> str:
    if not isinstance(ct, str) or not ct.strip():
        return "missing"
    main = ct.split(";")[0].strip().lower().split("/")[0]
    return main if main in ("image", "text", "video", "application") else "other"


def byte_bucket(n: int) -> str:
    return ("lt_50k" if n < 50_000 else "50k_200k" if n < 200_000 else "200k_1m" if n < 1_000_000
            else "1m_5m" if n < 5_000_000 else "gt_5m")


def image_http_word(status: int) -> str:
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if 300 <= status < 400:
        return "redirect"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server"
    return "client_error"


# ════════════════════════════════════════════════════════════════════
# 汇总与结论（试跑前定死，跑完不改）
# ════════════════════════════════════════════════════════════════════

def wilson(k: int, n: int, z: float = 1.96) -> list:
    if n <= 0:
        return [0.0, 1.0]
    p = k / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (centre - adj) / den), 3), round(min(1.0, (centre + adj) / den), 3)]


def _stats(xs: list) -> dict:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {"min": None, "median": None, "max": None}
    m = len(xs)
    med = xs[m // 2] if m % 2 else (xs[m // 2 - 1] + xs[m // 2]) / 2
    return {"min": round(xs[0], 3), "median": round(med, 3), "max": round(xs[-1], 3)}


def _count(xs: Iterable) -> dict:
    return dict(sorted(Counter(xs).items()))


def v1_verdict(calls: list) -> tuple:
    ok = [c for c in calls if c["status"] == "ok"]
    denom = [c for c in ok if not (c.get("is_video") and c.get("path") == "absent")]
    if not denom:
        return "no_data", 0
    if any(not c.get("v1_ok") for c in denom):
        return "differs", len(denom)
    return ("confirmed" if len(denom) >= 3 else "too_few"), len(denom)


def v2a_verdict(calls: list) -> tuple:
    """「no」只在顺序真的对不上时 (后面一项的号比前面小)；缺号、重号、0 起和 1 起混着来都说明不了 → unknown。"""
    multi = [c for c in calls if c["status"] == "ok" and (c.get("n_images") or 0) >= 2]
    words = [c.get("index_order") for c in multi]
    if any(w == "unsorted" for w in words):
        return "no", len(multi)
    if len(multi) >= 3 and (all(w == "sequential_0" for w in words) or all(w == "sequential_1" for w in words)):
        return "yes", len(multi)
    return "unknown", len(multi)


def _item0_match(a: dict) -> bool:
    return a.get("d0") is not None and a["d0"] <= ASPECT_TOL


# 只拿「飞书里只有一张图」的对照下结论（owner 定的、I-36：多图的帖飞书第一张是不是封面没人确认）。
# 佐证要的是能分辨的匹配：第 0 项和飞书首图宽高比对得上、别的项对不上；全是 3:4 那种（并列）、只有一张图的，说明不了什么。
_V2B_DISCRIM = ("item0_unique_match",)
_V2B_MISMATCH = ("no_match", "single_image_no_match")
_V2B_NONDISCRIM = ("item0_shared_match", "single_image_match", "no_dims")


def v2b_verdict(calls: list) -> dict:
    ctl = [c for c in calls if c["pool"] == "control" and c["status"] == "ok" and c.get("aspect")]
    single = [c for c in ctl if c.get("single")]
    multi = [c for c in ctl if not c.get("single")]
    cat = lambda c: c["aspect"]["category"]  # noqa: E731
    corroborating = sum(1 for c in single if cat(c) in _V2B_DISCRIM and c["aspect"].get("discriminating"))
    other_single = sum(1 for c in single if cat(c) == "other_match_only")
    mismatch = sum(1 for c in single if cat(c) in _V2B_MISMATCH)
    nondiscrim = sum(1 for c in single if cat(c) in _V2B_NONDISCRIM
                     or (cat(c) in _V2B_DISCRIM and not c["aspect"].get("discriminating")))
    n = len(single)
    if other_single >= 2:
        v = "contradicts"
    elif corroborating >= 2 and other_single == 0 and mismatch * 2 <= n:
        v = "corroborates"
    elif n == 0 or (corroborating < 2 and (mismatch + nondiscrim) * 2 > n):
        v = "controls_uninformative"
    else:
        v = "inconclusive"
    return {"verdict": v, "evaluated": len(ctl), "evaluated_single": n, "corroborating_single": corroborating,
            "other_match_only_single": other_single, "mismatch_single": mismatch, "nondiscriminating_single": nondiscrim,
            # 多图对照只记数、不动结论（第一张是不是封面本身没确认）
            "other_match_only_multi": sum(1 for c in multi if cat(c) == "other_match_only"),
            "item0_match_multi": sum(1 for c in multi if cat(c) in ("item0_unique_match", "item0_shared_match"))}


def v2_verdict(v2a: str, v2b: str) -> str:
    if v2a == "no" or v2b == "contradicts":
        return "no"
    if v2a == "yes" and v2b == "corroborates":
        return "yes"
    return "unsettled"


def v3_verdict(attempted: int, ok: int, disabled: bool) -> str:
    if disabled:
        return "no"
    if attempted >= 3:
        share = ok / attempted
        return "yes" if share >= 0.90 else ("partial" if share >= 0.50 else "no")
    return "not_tested"


def recheck_verdict(words: list, same: list) -> str:
    """not_attempted = 图片请求的上限用完了、没去重下：不是链接过期，不算 degraded（只在计数里看得到）。"""
    capped = any(w == "not_attempted" for w in words)
    words = [w for w in words if w not in ("skipped", "skipped_time", "na", "not_attempted")]
    if not words:
        return "cap_reached" if capped else "skipped"
    if any(w != "ok" for w in words):
        return "degraded"
    return "stable" if all(s is True for s in same) else "stable_bytes_changed"


def recommendation(status: str, n_ok: int, v1: str, v2: str, v3: str) -> tuple:
    """第一条对上的为准。"""
    if status == "planned":
        return "na", "plan_only"
    if status in ("refused", "crashed") or n_ok < 4:
        return "do_not_ask_yet", "run_incomplete"
    if v1 != "confirmed":
        return "do_not_ask_yet", "shape_differs"
    if v2 == "no":
        return "do_not_ask_yet", "cover_no"
    if v3 == "no":
        return "do_not_ask_yet", "download_blocked"
    if v3 == "not_tested":
        return "do_not_ask_yet", "download_untested"
    if v2 == "unsettled":
        return "do_not_ask_yet", "cover_unsettled"
    return "ask_owner_full_run", "all_conditions_met"


CAVEATS = ("aspect_evidence_weak", "approved_similarity_comparison_not_done", "controls_from_two_projects",
           "alive_as_of_weeks_ago", "balance_diff_whole_account", "pixels_not_compared", "video_share_unknown",
           "short_no_id_not_covered")


def summarize(st: dict) -> dict:
    """st：运行状态（calls 记录、下载汇总、形状、选样、记账、余额、复查）。返回报告里 q1 起的各节和 verdicts。"""
    calls = st["calls"]
    shape: Shape = st["shape"]
    dl = st["dl"]
    ok_calls = [c for c in calls if c["status"] == "ok"]
    v1, v1_n = v1_verdict(calls)
    v2a, v2a_n = v2a_verdict(calls)
    v2b = v2b_verdict(calls)
    v2 = v2_verdict(v2a, v2b["verdict"])
    attempted = sum(1 for c in ok_calls if (c.get("download") or {}).get("attempted"))
    dl_ok = sum(1 for c in ok_calls if (c.get("download") or {}).get("status") == "ok")
    v3 = v3_verdict(attempted, dl_ok, dl["disabled"])
    rw = [c.get("recheck") for c in calls if c.get("recheck") not in (None, "na")]
    rs = [c.get("recheck_same_bytes") for c in calls if c.get("recheck") == "ok"]
    v3r = recheck_verdict(rw, rs)
    expiry = "present" if dl["expiry_like"] else "absent"
    tg = [c for c in calls if c["pool"] == "target"]
    gh = sum(1 for c in tg if c["status"] in ("gone", "hidden"))
    rec, why = recommendation(st["status"], len(ok_calls), v1, v2, v3)
    ctl = [c for c in calls if c["pool"] == "control" and c.get("aspect")]

    def cat_by(pred):
        return _count(c["aspect"]["category"] for c in ctl if pred(c))

    out = {
        "q1_shape": {"path": dict(shape.path), "envelope_keys": dict(shape.keys["envelope"]),
                     "data_keys": dict(shape.keys["data"]), "data0_keys": dict(shape.keys["data0"]),
                     "note_keys": dict(shape.keys["note"]), "item_keys": dict(shape.item_keys),
                     "item0_types": {k: dict(v) for k, v in shape.item0_types.items()},
                     "item0_subkeys": {k: dict(v) for k, v in shape.item0_subkeys.items()},
                     "cover_like_keys": {k: dict(v) for k, v in shape.cover_like.items()},
                     "v1_failures": dict(shape.v1_fail), "odd_keys": shape.odd, "truncated_keys": shape.truncated,
                     "ok_posts_counted": v1_n, "verdict": v1},
        "q2_order": {"counts": _count(c.get("index_order") for c in ok_calls), "eligible_posts": v2a_n, "verdict": v2a},
        "q2_aspect": {"tolerance": ASPECT_TOL, "by_category": cat_by(lambda c: True),
                      "single": cat_by(lambda c: c.get("single")), "multi": cat_by(lambda c: not c.get("single")),
                      "tall": cat_by(lambda c: c["stratum"].endswith("_tall")),
                      "ratio_3_4": cat_by(lambda c: c["stratum"].endswith("_ratio_3_4")),
                      "d0": _stats([c["aspect"].get("d0") for c in ctl]),
                      "d_other_min": _stats([c["aspect"].get("d_other_min") for c in ctl]),
                      "discriminating": sum(1 for c in ctl if c["aspect"].get("discriminating")),
                      "best_index": _count(c["aspect"].get("best_index") for c in ctl),
                      "origin_vs_item": _count(c["aspect"].get("origin_vs_item") for c in ctl),
                      **{k: v2b[k] for k in ("evaluated", "evaluated_single", "corroborating_single",
                                             "other_match_only_single", "mismatch_single", "nondiscriminating_single",
                                             "other_match_only_multi", "item0_match_multi")},
                      "verdict": v2b["verdict"], "strength": "weak"},
        "q2_size": {"size_match_index": _count(c.get("size_match_index") for c in ctl), "strength": "exploratory"},
        "video": {"type_counts": _count(c.get("note_type") for c in ok_calls),
                  "model_type_counts": _count(c.get("model_type") for c in ok_calls),
                  "video_notes": sum(1 for c in ok_calls if c.get("is_video")),
                  "video_cover_keys": dict(shape.video_cover),
                  "video_images_list_len": _count(n_images_bucket(c.get("n_images")) for c in ok_calls if c.get("is_video")),
                  "video_item0_present": sum(1 for c in ok_calls if c.get("is_video") and (c.get("n_images") or 0) >= 1)},
        "q3_download": {"attempted": attempted, "ok": dl_ok,
                        "status_counts": _count((c.get("download") or {}).get("status", "not_attempted") for c in ok_calls),
                        "tier_needed": dl["tier_needed"] or "none", "disabled": dl["disabled"],
                        "refused_posts": dl["refused_posts"], "probes": dl["probes"],
                        "ct_family": _count(c["download"]["ct_family"] for c in ok_calls if c.get("download")),
                        "format": _count(c["download"]["format"] for c in ok_calls if c.get("download")),
                        "bytes": _count(c["download"]["bytes"] for c in ok_calls if c.get("download")),
                        "url_key": _count(c["download"]["url_key"] for c in ok_calls if c.get("download")),
                        "scheme": _count(c["download"]["scheme"] for c in ok_calls if c.get("download")),
                        "scheme_used": _count(c["download"]["scheme_used"] for c in ok_calls if c.get("download")),
                        "redirected": sum(c["download"].get("redirected", 0) for c in ok_calls if c.get("download")),
                        "hosts_distinct": dl["hosts_distinct"], "registrable_distinct": dl["registrable_distinct"],
                        "xhs_named": dl["xhs_named"], "on_known_xhs_domain": dl["on_known_xhs_domain"],
                        "param_names": dl["param_names"], "odd_params": dl["odd_params"], "expiry_like": expiry,
                        "expiry_value_shapes": dl["expiry_value_shapes"], "path_segments": dl["path_segments"],
                        "bytes_vs_json_aspect": _count(c["download"]["bytes_vs_json_aspect"] for c in ok_calls
                                                       if c.get("download")),
                        "video_downloads": sum(1 for c in ok_calls if (c.get("download") or {}).get("video")),
                        "recheck_minutes": st["recheck_minutes"], "recheck": _count(rw),
                        "recheck_same_bytes": {"yes": sum(1 for s in rs if s is True), "no": sum(1 for s in rs if s is False)},
                        "verdict": v3, "recheck_verdict": v3r},
        "q4_targets": {"by_link": {lk: _count(c["status"] for c in tg if c["stratum"] == lk)
                                   for lk in ("long_explore", "long_discovery", "short_with_id")},
                       "sent": len(tg), "gone": sum(1 for c in tg if c["status"] == "gone"),
                       "hidden": sum(1 for c in tg if c["status"] == "hidden"),
                       "ok": sum(1 for c in tg if c["status"] == "ok"),
                       "other": sum(1 for c in tg if c["status"] not in ("ok", "gone", "hidden")),
                       "gone_or_hidden_share": round(gh / len(tg), 3) if tg else None,
                       "wilson95": wilson(gh, len(tg)) if tg else None, "note": "indicative_n_le_4"},
        "verdicts": {"q1": v1, "q2_order": v2a, "q2_aspect": v2b["verdict"], "q2": v2, "q3": v3, "q3_recheck": v3r,
                     "expiry_markers": expiry, "q4": "indicative_n_le_4", "approved_similarity_comparison": "not_done",
                     "recommendation": rec, "recommendation_reason": why},
    }
    return out


# ════════════════════════════════════════════════════════════════════
# 输出清洗：报告里的字符串只许是固定词表、数字、键名
# ════════════════════════════════════════════════════════════════════

VOCAB = frozenset(
    {SCHEMA, IMAGES_PATH, "absent", "plan", "run", "rehearse", "planned", "completed", "stopped", "refused", "crashed",
     "done", "cap_reached", "consecutive_failures", "unknown_shape_call1", "shape_absent", "selection_failed",
     "refused_missing_key", "refused_missing_token", "refused_window", "refused_drift", "refused_pool", "balance_auth",
     "balance_unreadable", "balance_disabled", "balance_low", "refused_claimed", "refused_claim_error", "exception",
     "json_paths", "raw_extra_fallback", "rehearse_fake", "missing_env", "not_json", "not_list", "too_large",
     "bank_state_empty", "ok", "drift", "open", "crawl_window", "control", "target", "na", "none", "other",
     "normal", "video", "multi", "note", "error", "item", "origin_img", "match", "mismatch", "unparsed", "skipped",
     "skipped_time", "disabled", "image_cap", "https", "http", "image", "text", "application", "missing", "webp", "jpeg",
     "png", "gif", "heic", "avif", "lt_50k", "50k_200k", "200k_1m", "1m_5m", "gt_5m", "confirmed", "differs",
     "too_few", "no_data", "yes", "no", "unknown", "contradicts", "corroborates", "controls_uninformative",
     "inconclusive", "unsettled", "partial", "not_tested", "stable", "stable_bytes_changed", "degraded", "present",
     "ask_owner_full_run", "do_not_ask_yet", "run_incomplete", "shape_differs", "cover_no", "download_blocked",
     "download_untested", "cover_unsettled", "all_conditions_met", "plan_only", "weak", "exploratory", "whole_account_shared",
     "low", "unreadable", "indicative_n_le_4", "not_done", "not_called", "no_size", "3_plus", "0", "1", "2", "2_3", "4_6",
     "7_plus", "le_tol", "tol_to_0_05", "0_05_to_0_2", "gt_0_2", "xhs_more", "equal", "xhs_fewer", "scheme",
     "userinfo", "port", "ip", "host", "images_list_absent", "images_list_empty", "item_not_dict", "no_index",
     "no_width", "no_height", "no_url", "str", "str_url", "int", "float", "bool", "null", "list", "dict", "empty",
     "single", "sequential_0", "sequential_1", "sorted_gaps", "unsorted", "ties", "non_int", "flex", "cap_reached"}
    | set(STATUS_WORDS) | set(IMAGE_WORDS) | set(TIERS) | set(URL_KEYS) | set(CALL_ORDER) | set(CAVEATS)
    | {f"{a}_{b}" for a in ("single", "multi") for b in ("tall", "ratio_3_4", "other")}
    | {"long_explore", "long_discovery", "short_with_id", "short_no_id"}
    | {"item0_unique_match", "item0_shared_match", "other_match_only", "no_match", "single_image_match", "single_image_no_match", "no_dims"})
_VALUE_PATTERNS = (re.compile(r"(digits|hex|alnum|b64ish|other)_(1_8|9_16|17_32|33_plus)"),
                   re.compile(r"\d{4}-\d{2}-\d{2}"), re.compile(r"http_\d{3}"),
                   re.compile(r"[A-Z][A-Za-z0-9]{1,40}(Error|Exception|Exit|Interrupt|Warning|Reached|Failed|Timeout)"))


def _ok_key(k: Any) -> bool:
    if not isinstance(k, str):
        return False
    if k in VOCAB or re.fullmatch(r"\d{1,3}", k):
        return True
    parts = k.split(".")
    if 1 <= len(parts) <= 4 and all(safe_key(p) for p in parts):
        return True
    return safe_param(k)


def _ok_value(v: str) -> bool:
    return v in VOCAB or any(p.fullmatch(v) for p in _VALUE_PATTERNS)


def scrub(obj: Any) -> tuple:
    """(清洗后的对象, 换掉了几处)。键要是词表 / 键名 / 参数名，字符串值只许是词表、形状类、日期、异常类名；别的一律换成 redacted。
    这是最后一道兜底：代码本身不该产出任何不合格的字符串（测试要求 scrubbed == 0）。"""
    n = [0]

    def walk(x):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                kk = k if _ok_key(k) else None
                if kk is None:
                    n[0] += 1
                    kk = f"redacted_{n[0]}"
                out[kk] = walk(v)
            return out
        if isinstance(x, (list, tuple)):
            return [walk(v) for v in x]
        if isinstance(x, str):
            if _ok_value(x):
                return x
            n[0] += 1
            return "redacted"
        if x is None or isinstance(x, (bool, int)):
            return x
        if isinstance(x, float):
            return x if math.isfinite(x) else None
        n[0] += 1
        return "redacted"

    return walk(obj), n[0]


_HEX12 = re.compile(r"[0-9a-fA-F]{12,}")
_DIG9 = re.compile(r"\d{9,}")


def safe_line(line: str, secrets: Iterable = ()) -> str:
    """公开日志的最后一道闸：带 ://、12 位以上十六进制、9 位以上数字、xsec、@ 或任何密钥的行整行换掉。"""
    low = line.lower()
    if ("://" in line or _HEX12.search(line) or _DIG9.search(line) or "xsec" in low or "@" in line
            or any(s and s in line for s in secrets)):
        return "[redacted line]"
    return line
