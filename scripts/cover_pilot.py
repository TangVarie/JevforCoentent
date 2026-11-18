#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""I-35 封面试点（手动、一次性）：没有飞书首图的爆帖，封面能不能从发布链接经 TikHub 详情接口拿到。说明见 docs/cover-pilot-2026-11-18.md。

  python3 scripts/cover_pilot.py plan --out out          # 只读 TV（SELECT），打印计数和 10 个槽位；不碰 TikHub、GitHub、图片服务器
  python3 scripts/cover_pilot.py rehearse --out out      # 整条付费路径，但所有外部请求都换成进程内的假服务；不读任何环境变量、不联网
  python3 scripts/cover_pilot.py run --out out --summary "$GITHUB_STEP_SUMMARY" --recheck-minutes 20
                                                         # 付费：最多 10 次详情（¥0.75），每次发之前查上限；一次性（git tag 认领）

本仓、Actions 日志、step summary、产物都是公开的：输出只有计数、固定状态词、键名、形状串和宽高比统计。
笔记 id、链接、主机名、标题、昵称、项目代号、file token、返回体、cache_url、余额（只出差值）、密钥一概不打、不写。
退出码：0 完成 · 3 按停止规则提前停（报告有效）· 2 一分钱没花就拒绝 · 1 出错（finally 里照样写出部分报告）。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import math
import os
import re
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections import Counter, namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from judge import external as E  # noqa: E402
from scripts import cover_pilot_core as K  # noqa: E402

TIKHUB_BASE = "https://api.tikhub.io"           # 写死：不让任何环境变量改主机（judge/external.py 那个覆盖变量这里不读）
DETAIL_PATH = E.DETAIL_PATH
BALANCE_PATH = "/api/v1/tikhub/user/get_user_info"   # TikHub 标明 endpoint_cost 0（linkcheck xhsearch/balance.py，08-26 核过）
GITHUB_REFS = "https://api.github.com/repos/{repo}/git/refs"
DETAIL_TIMEOUT, DETAIL_CAP, DETAIL_GAP = 60, 4 * 1024 * 1024, 1.5    # 60 秒同 judge/external.py：超时的那次照样计费
IMAGE_TIMEOUT, IMAGE_CAP, IMAGE_GAP = 20, 15 * 1024 * 1024, 0.5
SUPA_TIMEOUT, SUPA_CAP, PAGE, RAW_PAGE = 60, 64 * 1024 * 1024, 1000, 200
CHUNK = 64 * 1024
REPORT_JSON, REPORT_MD = "cover_pilot_report.json", "cover_pilot_report.md"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# PostgREST 路径里的中文键名 / 档位值要编码，否则 urllib 的请求行直接报错
SUPA_SAFE = "/?&=,.:()*-_>"

NOTES_JSON = ("notes?select=note_id,project_id,platform,tier,publish_url,"
              "wt:raw_extra->>作品ID,wu:raw_extra->_undeclared->>作品ID,"
              "pt:raw_extra->>巡查状态,pu:raw_extra->_undeclared->>巡查状态,"
              "at:raw_extra->已确认存活,au:raw_extra->_undeclared->已确认存活"
              "&tier=in.(爆,大爆)&order=note_id")
NOTES_RAW = "notes?select=note_id,project_id,platform,tier,publish_url,raw_extra&tier=in.(爆,大爆)&order=note_id"
SOURCE_Q = "v_note_first_image_source?select=note_id,n_files,file_sha&order=note_id"          # 不取 first_file
FI_Q = "note_first_image?select=note_id,width,height,status,n_files,file_sha,file_kind,file_bytes&order=note_id"
BANK_Q = "first_image_bank_state?select=excluded_projects"
VANISHED_Q = "v_notes_vanished?select=note_id&order=note_id"     # = TV _common.fetch_vanished_note_ids

Resp = namedtuple("Resp", "status headers body flag")      # flag: "" | too_large | ct_stopped
NETWORK_EXC = (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException,
               socket.timeout, OSError)
_SECRETS: list = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 不跟：带着 Bearer 头跟到别的主机是泄露；图片要跟的话由调用方自己判断（同域、过链接闸、最多 2 跳）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def say(line: str) -> None:
    print(K.safe_line(str(line), _SECRETS), flush=True)


def exc_word(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if K._VALUE_PATTERNS[3].fullmatch(name) else "UnlistedException"


def _on_term(signum, frame):
    raise SystemExit(143)


def _harden() -> Callable:
    """日志 / 警告 / SIGTERM 的卫生，只在 main() 里开、结束时还原（不在 import 时改全局状态）。
    urllib3 在 WARNING 级把整条 URL 打出来（图片的 sign / t、TikHub 的 note_id），ResourceWarning 会带对端 IP。"""
    saved = []
    for name in ("urllib3", "urllib3.connectionpool", "requests"):
        lg = logging.getLogger(name)
        h = logging.NullHandler()
        saved.append((lg, lg.level, lg.propagate, h))
        lg.addHandler(h)
        lg.setLevel(logging.ERROR)
        lg.propagate = False
    root_null = logging.NullHandler()
    logging.getLogger().addHandler(root_null)
    old_raise = logging.raiseExceptions
    logging.raiseExceptions = False
    old_unraisable = sys.unraisablehook
    sys.unraisablehook = lambda unraisable: None
    wctx = warnings.catch_warnings()
    wctx.__enter__()
    warnings.filterwarnings("ignore")
    try:
        old_term = signal.signal(signal.SIGTERM, _on_term)     # GitHub 取消 job 发 SIGTERM：照样走到 finally 写报告
    except (ValueError, OSError):
        old_term = None

    def restore() -> None:
        if old_term is not None:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass
        wctx.__exit__(None, None, None)
        sys.unraisablehook = old_unraisable
        logging.raiseExceptions = old_raise
        logging.getLogger().removeHandler(root_null)
        for lg, level, prop, h in saved:
            lg.removeHandler(h)
            lg.setLevel(level)
            lg.propagate = prop

    return restore


# ════════════════════════════════════════════════════════════════════
# 唯一的传输函数（测试整个换掉它）
# ════════════════════════════════════════════════════════════════════

def _close_quietly(obj: Any) -> None:
    try:
        obj.close()
    except Exception:  # noqa: BLE001
        pass


def http_fetch(url: str, headers: dict, *, timeout: float, cap: int, method: str = "GET", data: Optional[bytes] = None,
               stop_ct: Optional[Callable] = None) -> Resp:
    """不跟跳转、不带 cookie、按块读、超上限就停。非 2xx 不抛，回 Resp(状态码, 头, b"", "")。
    网络错误照原样抛（调用方只看类名）；HTTPError / URLError 的消息和 .url 带 URL，任何地方都不格式化它们。"""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        hdrs = {str(k).lower(): v for k, v in (e.headers.items() if e.headers is not None else [])}
        _close_quietly(e)
        return Resp(e.code, hdrs, b"", "")
    try:
        status = getattr(r, "status", None) or r.getcode()
        hdrs = {str(k).lower(): v for k, v in r.headers.items()}
        if stop_ct is not None and stop_ct(hdrs.get("content-type")):
            return Resp(status, hdrs, b"", "ct_stopped")
        cl = str(hdrs.get("content-length") or "").strip()
        if cl.isdigit() and int(cl) > cap:
            return Resp(status, hdrs, b"", "too_large")
        buf = bytearray()
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            buf += chunk
            if len(buf) > cap:
                return Resp(status, hdrs, b"", "too_large")
        return Resp(status, hdrs, bytes(buf), "")
    finally:
        _close_quietly(r)


def _resp_from_http_error(e: urllib.error.HTTPError) -> Resp:
    hdrs = {str(k).lower(): v for k, v in (e.headers.items() if getattr(e, "headers", None) is not None else [])}
    _close_quietly(e)
    return Resp(e.code, hdrs, b"", "")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    fetch: Callable
    sleep: Callable
    clock: Callable
    now: Callable
    env: Any
    source_word: Optional[str] = None


def default_deps() -> Deps:
    return Deps(fetch=lambda *a, **k: http_fetch(*a, **k), sleep=lambda s: time.sleep(s), clock=lambda: time.monotonic(),
                now=_utcnow, env=os.environ)


# ════════════════════════════════════════════════════════════════════
# TV（只读 GET）
# ════════════════════════════════════════════════════════════════════

def supabase_get(fetch: Callable, base: str, key: str, path: str) -> list:
    full = f"{base.rstrip('/')}/rest/v1/{quote(path, safe=SUPA_SAFE)}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault", "Accept": "application/json"}
    try:
        r = fetch(full, headers, timeout=SUPA_TIMEOUT, cap=SUPA_CAP)
    except urllib.error.HTTPError as e:
        raise K.SelectionFailed(f"http_{_resp_from_http_error(e).status}") from None
    except Exception as e:  # noqa: BLE001
        raise K.SelectionFailed(exc_word(e)) from None
    if r.status != 200:
        raise K.SelectionFailed(f"http_{r.status}")
    if r.flag:
        raise K.SelectionFailed("too_large")
    try:
        data = json.loads(r.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise K.SelectionFailed("not_json") from None
    if not isinstance(data, list):
        raise K.SelectionFailed("not_list")
    return data


def pages(fetch: Callable, base: str, key: str, q: str, size: int = PAGE) -> list:
    out, offset = [], 0
    while True:
        page = supabase_get(fetch, base, key, f"{q}&limit={size}&offset={offset}")
        out.extend(page)
        if len(page) < size:
            return out
        offset += size


def _norm_json(r: dict) -> dict:
    return {"note_id": r.get("note_id"), "project_id": r.get("project_id"), "platform": r.get("platform"),
            "tier": r.get("tier"), "publish_url": r.get("publish_url"), "wid_top": r.get("wt"), "wid_und": r.get("wu"),
            "patrol_top": r.get("pt"), "patrol_und": r.get("pu"), "alive_top": r.get("at"), "alive_und": r.get("au")}


def _norm_raw(r: dict) -> dict:
    ex = r.get("raw_extra") if isinstance(r.get("raw_extra"), dict) else {}
    und = ex.get("_undeclared") if isinstance(ex.get("_undeclared"), dict) else {}
    return {"note_id": r.get("note_id"), "project_id": r.get("project_id"), "platform": r.get("platform"),
            "tier": r.get("tier"), "publish_url": r.get("publish_url"), "wid_top": ex.get("作品ID"),
            "wid_und": und.get("作品ID"), "patrol_top": ex.get("巡查状态"), "patrol_und": und.get("巡查状态"),
            "alive_top": ex.get("已确认存活"), "alive_und": und.get("已确认存活")}


def read_tv(fetch: Callable, base: str, key: str) -> dict:
    """五个只读 GET；任何一个失败抛 SelectionFailed（一分钱没花就停）。raw_extra 退路只在内存里取几个键，绝不打印。"""
    source_word = "json_paths"
    try:
        notes = [_norm_json(r) for r in pages(fetch, base, key, NOTES_JSON)]
        if not any(n["wid_top"] is not None or n["wid_und"] is not None for n in notes):
            raise K.SelectionFailed("http_400")
    except K.SelectionFailed as exc:
        if exc.args[:1] != ("http_400",):
            raise
        source_word = "raw_extra_fallback"
        notes = [_norm_raw(r) for r in pages(fetch, base, key, NOTES_RAW, RAW_PAGE)]
    source = {r.get("note_id"): {"n_files": r.get("n_files"), "file_sha": r.get("file_sha")}
              for r in pages(fetch, base, key, SOURCE_Q)}
    fi = {r.get("note_id"): r for r in pages(fetch, base, key, FI_Q)}
    bank = supabase_get(fetch, base, key, BANK_Q)
    if not bank or not isinstance(bank[0], dict):
        raise K.SelectionFailed("bank_state_empty")
    excluded = set(bank[0].get("excluded_projects") or [])
    vanished = {r.get("note_id") for r in pages(fetch, base, key, VANISHED_Q)}
    return {"notes": notes, "source": source, "fi": fi, "excluded": excluded, "vanished": vanished, "source_word": source_word}


# ════════════════════════════════════════════════════════════════════
# TikHub / GitHub
# ════════════════════════════════════════════════════════════════════

def _tikhub_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": E.USER_AGENT}


def tikhub_detail(fetch: Callable, key: str, xid: str) -> tuple:
    """一次付费详情调用 → (状态词, 解析后的返回 或 None)。不重试。"""
    url = f"{TIKHUB_BASE}{DETAIL_PATH}?{urlencode({'note_id': xid})}"
    try:
        r = fetch(url, _tikhub_headers(key), timeout=DETAIL_TIMEOUT, cap=DETAIL_CAP)
    except urllib.error.HTTPError as e:
        code = _resp_from_http_error(e).status
        return (K.http_word(code) if code != 200 else "unknown_shape"), None
    except Exception:  # noqa: BLE001 — 发没发出去不知道：照样算钱、记 network，不重试
        return "network", None
    if r.flag == "too_large":
        return "unknown_shape", None
    return K.classify(r.status, r.body)


def tikhub_balance(fetch: Callable, key: str) -> tuple:
    """零费用的余额端点 → (ok | auth | unreadable | disabled, 美元余额)。只读 user_data.balance 和 account_disabled，别的当场丢。"""
    try:
        r = fetch(TIKHUB_BASE + BALANCE_PATH, _tikhub_headers(key), timeout=30, cap=1024 * 1024)
    except urllib.error.HTTPError as e:
        return ("auth" if _resp_from_http_error(e).status in (401, 403) else "unreadable"), None
    except NETWORK_EXC:
        return "unreadable", None
    if r.status in (401, 403):
        return "auth", None
    if r.status != 200 or r.flag:
        return "unreadable", None
    try:
        data = json.loads(r.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "unreadable", None
    user = data.get("user_data") if isinstance(data, dict) else None
    if not isinstance(user, dict) and isinstance(data, dict) and isinstance(data.get("data"), dict):
        user = data["data"].get("user_data")
    if not isinstance(user, dict):
        return "unreadable", None
    try:
        bal = float(user.get("balance"))
    except (TypeError, ValueError):
        return "unreadable", None
    if not math.isfinite(bal):
        return "unreadable", None
    if user.get("account_disabled") is True:
        return "disabled", None
    return "ok", bal


def claim_run(repo: str, token: str, sha: str, post: Optional[Callable] = None, fetch: Optional[Callable] = None) -> str:
    """把这唯一一次付费运行用掉：建 git tag CLAIM_TAG（GitHub refs API，原子的，已存在回 422）。
    和 scripts/evaluate_comment_gate.py 的 github_claim 同一套语义，用标准库（requests 没装）。
    回 claimed | refused_claimed | refused_claim_error；除了 201 一律拒（fail closed）。"""
    if not (token and REPO_RE.match(repo or "") and SHA_RE.match(sha or "")):
        return "refused_claim_error"
    if post is None:
        send = fetch or http_fetch

        def post(url: str, payload: dict) -> int:
            return send(url, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                              "Content-Type": "application/json", "User-Agent": E.USER_AGENT},
                        timeout=30, cap=1024 * 1024, method="POST", data=json.dumps(payload).encode("utf-8")).status
    try:
        status = post(GITHUB_REFS.format(repo=repo), {"ref": f"refs/tags/{K.CLAIM_TAG}", "sha": sha})
    except urllib.error.HTTPError as e:
        status = _resp_from_http_error(e).status
    except Exception:  # noqa: BLE001
        return "refused_claim_error"
    if status == 201:
        return "claimed"
    if status == 422:
        return "refused_claimed"
    return "refused_claim_error"


# ════════════════════════════════════════════════════════════════════
# 图片下载（Q3）
# ════════════════════════════════════════════════════════════════════

def tier_headers(tier: str) -> dict:
    h = {"Accept": K.IMAGE_ACCEPT}
    if tier in ("browser_ua", "browser_ua_referer"):
        h["User-Agent"] = K.BROWSER_UA
    if tier == "browser_ua_referer":
        h["Referer"] = K.XHS_REFERER
    return h


def _video_ct(ct: Any) -> bool:
    return K.ct_family(ct) == "video"


class Downloader:
    """item 0 的图：找到能用的请求头档位就固定下来；没找到之前每篇都从头试（图片请求不花钱）。
    只有在 ≥2 篇上各档都被明确拒（403 / 404 / 不是图 / 疑似占位图 / 跨域跳转）才停止下载；网络、5xx、太大不算。
    链接只在内存里（复查要用），不写盘、不打印。"""

    def __init__(self, ledger: K.Ledger, fetch: Callable, sleep: Callable, clock: Callable):
        self.ledger, self.fetch, self.sleep, self.clock = ledger, fetch, sleep, clock
        self.working: Optional[tuple] = None
        self.tier_needed: Optional[str] = None
        self.disabled = False
        self.cap_hit = False
        self._post_sent = False
        self.refused_posts = 0
        self.probes: list = []
        self._last: Optional[float] = None
        self._memory: dict = {}
        self._hosts: set = set()
        self._regs: set = set()
        self.xhs_named: Counter = Counter()
        self.on_known: Counter = Counter()
        self.param_names: Counter = Counter()
        self.odd_params = 0
        self.expiry_like = False
        self.expiry_shapes: dict = {}
        self.path_segments: Counter = Counter()

    def _pace(self) -> None:
        if self._last is not None:
            wait = IMAGE_GAP - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _fetch(self, url: str, tier: str, dims: Optional[tuple]) -> tuple:
        cur, redirected = url, 0
        while True:
            self.ledger.charge_image()
            self._post_sent = True                                # 这一篇真的发出过请求（上限在跳转中途用完时也要算「试过」）
            self._pace()
            try:
                r = self.fetch(cur, tier_headers(tier), timeout=IMAGE_TIMEOUT, cap=IMAGE_CAP, stop_ct=_video_ct)
            except urllib.error.HTTPError as e:
                r = _resp_from_http_error(e)
            except NETWORK_EXC:
                return "network", {"redirected": redirected}
            if 300 <= r.status < 400:
                loc = r.headers.get("location")
                nxt = urljoin(cur, loc.strip()) if isinstance(loc, str) and loc.strip() else None
                if (nxt and redirected < 2 and K.url_gate(nxt)[0]
                        and K.registrable(K.host_of(nxt)) == K.registrable(K.host_of(cur))):
                    redirected += 1
                    cur = nxt
                    continue
                return "redirect", {"redirected": redirected}
            info = {"redirected": redirected, "ct_family": K.ct_family(r.headers.get("content-type"))}
            if r.flag == "ct_stopped":
                return "not_image", info
            if r.status != 200:
                return K.image_http_word(r.status), info
            if r.flag == "too_large":
                return "too_large", info
            body = r.body or b""
            info.update(format=K.sniff(body), bytes=K.byte_bucket(len(body)))
            if info["format"] == "other":
                return "not_image", info
            info["bytes_vs_json_aspect"] = K.bytes_vs_json(body, dims)
            info["_sha"] = hashlib.sha256(body).hexdigest()       # 只在内存里比复查前后是不是同一份字节，不输出
            return ("placeholder_suspect" if info["bytes_vs_json_aspect"] == "mismatch" else "ok"), info

    @staticmethod
    def _variants(url: str) -> list:
        if urlsplit(url).scheme.lower() == "https":
            return [(url, "https")]
        return [(K.https_variant(url), "https"), (url, "http")]

    def _note_url(self, url: str) -> None:
        host = K.host_of(url)
        reg = K.registrable(host)
        self._hosts.add(host)
        self._regs.add(reg)
        self.xhs_named["yes" if K.xhs_named(host) else "no"] += 1
        self.on_known["yes" if reg in K.KNOWN_XHS_DOMAINS else "no"] += 1
        s = K.url_shape(url)
        self.param_names.update(s["param_names"])
        self.odd_params += s["odd_params"]
        self.expiry_like = self.expiry_like or s["expiry_like"]
        for k, v in s["expiry_shapes"].items():
            self.expiry_shapes.setdefault(k, Counter())[v] += 1
        self.path_segments[str(min(s["path_segments"], 99))] += 1

    def download(self, plan: dict, slot: str) -> dict:
        d = {"status": "not_attempted", "tier": "none", "probes": [], "scheme": "none", "scheme_used": "none",
             "ct_family": "none", "format": "none", "bytes": "none", "url_key": plan.get("key") or "none",
             "xhs_named": None, "on_known_xhs_domain": None, "bytes_vs_json_aspect": "na",
             "video": bool(plan.get("video")), "redirected": 0, "attempted": False, "reject": None,
             "not_attempted_reason": None}
        url = plan.get("url")
        if not url:
            d["status"] = "no_url"
            return d
        ok, why = K.url_gate(url)
        if not ok:
            d.update(status="url_rejected", reject=why)
            return d
        self._note_url(url)
        host = K.host_of(url)
        d.update(scheme=urlsplit(url).scheme.lower(), xhs_named=K.xhs_named(host),
                 on_known_xhs_domain=K.registrable(host) in K.KNOWN_XHS_DOMAINS)
        if self.disabled or self.cap_hit:
            d["not_attempted_reason"] = "disabled" if self.disabled else "image_cap"
            return d
        dims = plan.get("dims")
        last = None
        self._post_sent = False
        try:
            if self.working:
                tier, _sch = self.working
                # 档位固定下来之后，http 的链接照样先试 https、不行再试 http（不靠前一篇用的是哪种；Codex 11-18）
                for vurl, _s in self._variants(url):
                    word, info = self._fetch(vurl, tier, dims)
                    last = (word, info, vurl, tier)
                    if word == "ok":
                        break
            else:
                probes, nondet = [], False
                for tier in K.TIERS:
                    for vurl, sch in self._variants(url):
                        word, info = self._fetch(vurl, tier, dims)
                        probes.append({"tier": tier, "scheme": sch, "status": word})
                        last = (word, info, vurl, tier)
                        if word == "ok":
                            self.working = (tier, sch)
                            self.tier_needed = self.tier_needed or tier
                            break
                    if last[0] == "ok":
                        break
                    if last[0] not in K.DETERMINISTIC_REFUSALS:
                        nondet = True                     # 网络 / 5xx / 太大：和请求头无关，下一篇再从头试
                        break
                d["probes"] = probes
                if len(self.probes) < 4:
                    self.probes.append(probes)
                if last[0] != "ok" and not nondet:
                    self.refused_posts += 1
                    if self.refused_posts >= 2:
                        self.disabled = True
        except K.CapReached:
            self.cap_hit = True
            if last is None:
                if self._post_sent:                                # 发出过请求、跳转中途撞上限：算试过、没成（进 Q3 的分母）
                    d.update(status="cap_interrupted", attempted=True)
                    return d
                d["not_attempted_reason"] = "image_cap"
                return d
        word, info, vurl, tier = last
        d.update(status=word, tier=tier, attempted=True, scheme_used=urlsplit(vurl).scheme.lower(),
                 ct_family=info.get("ct_family", "none"), format=info.get("format", "none"),
                 bytes=info.get("bytes", "none"), bytes_vs_json_aspect=info.get("bytes_vs_json_aspect", "na"),
                 redirected=info.get("redirected", 0))
        if word == "ok":
            self._memory[slot] = (vurl, tier, info["_sha"], dims)
        return d

    def recheck(self, slot: str) -> tuple:
        vurl, tier, sha, dims = self._memory[slot]
        try:
            word, info = self._fetch(vurl, tier, dims)
        except K.CapReached:
            return "not_attempted", None
        return word, (info.get("_sha") == sha if word == "ok" else None)

    def summary(self) -> dict:
        return {"disabled": self.disabled, "tier_needed": self.tier_needed, "refused_posts": self.refused_posts,
                "probes": self.probes, "hosts_distinct": len(self._hosts), "registrable_distinct": len(self._regs),
                "xhs_named": {"yes": self.xhs_named["yes"], "no": self.xhs_named["no"]},
                "on_known_xhs_domain": {"yes": self.on_known["yes"], "no": self.on_known["no"]},
                "param_names": dict(self.param_names), "odd_params": self.odd_params, "expiry_like": self.expiry_like,
                "expiry_value_shapes": {k: dict(v) for k, v in self.expiry_shapes.items()},
                "path_segments": dict(self.path_segments)}


# ════════════════════════════════════════════════════════════════════
# 一次运行
# ════════════════════════════════════════════════════════════════════

class Refused(Exception):
    """一分钱没花就拒绝；args[0] 是固定的原因词。"""


def _new_rec(slot: str, cand: dict) -> dict:
    return {"slot": slot, "pool": cand["pool"], "stratum": cand["stratum"], "single": bool(cand.get("single")),
            "status": None, "note_type": "na", "model_type": "na", "is_video": False, "n_images": None,
            "index_order": "na", "n_files_cmp": "na", "path": "na", "v1_ok": None, "v1_fail": None, "aspect": None,
            "size_match_index": None, "analysis_error": None, "download": None, "recheck": "na",
            "recheck_same_bytes": None}


def _analyze(rec: dict, raw: dict, cand: dict, st: dict) -> None:
    """一篇 ok 的返回：形状、顺序、宽高比、视频、item 0 的链接。真实数据里的怪值（字符串宽高、None 项…）只记一个类名，不停整次运行。"""
    try:
        note = K.note_of(raw)
        info = st["shape"].add(raw, note)
        rec.update(path=info["path"], v1_ok=info["v1_ok"], v1_fail=info["v1_fail"], is_video=info["is_video"],
                   note_type=info["note_type"], model_type=info["model_type"])
        images = note.get("images_list") if isinstance(note.get("images_list"), list) else []
        rec["n_images"] = len(images)
        rec["index_order"] = K.index_order(images)
        if cand["pool"] == "control":
            rec["aspect"] = K.aspect_eval(images, cand.get("fs_w"), cand.get("fs_h"))
            rec["size_match_index"] = K.size_match_index(images, cand.get("fs_bytes"))
            rec["n_files_cmp"] = K.n_files_cmp(len(images), cand.get("n_files") or 0)
        item0 = images[0] if images else None
        url, key = K.choose_url(item0)
        rec["_dl"] = {"url": url, "key": key, "dims": K.item_dims(item0), "video": info["is_video"]}
    except Exception as exc:  # noqa: BLE001
        rec["analysis_error"] = exc_word(exc)


def _download(rec: dict, st: dict) -> None:
    plan = rec.pop("_dl", None)
    if plan is None:
        return
    try:
        rec["download"] = st["dl"].download(plan, rec["slot"])
    except Exception as exc:  # noqa: BLE001
        rec["download"] = {"status": "download_error", "tier": "none", "probes": [], "scheme": "none",
                           "scheme_used": "none", "ct_family": "none", "format": "none", "bytes": "none",
                           "url_key": plan.get("key") or "none", "xhs_named": None, "on_known_xhs_domain": None,
                           "bytes_vs_json_aspect": "na", "video": bool(plan.get("video")), "redirected": 0,
                           "attempted": False, "reject": None, "not_attempted_reason": None}
        rec["analysis_error"] = rec.get("analysis_error") or exc_word(exc)
    d = rec["download"]
    say(f"download {rec['slot']}: {d['status']} tier={d['tier']} {d['format']} {d['bytes']}")


def _balance_after(st: dict, deps: Deps, key: str) -> None:
    try:
        st["ledger"].charge_balance_read()
        word, bal = tikhub_balance(deps.fetch, key)
    except Exception:  # noqa: BLE001
        word, bal = "unreadable", None
    st["balance_after_word"] = word
    if word == "ok":
        st["balance_after"] = bal


def _pace_detail(st: dict, deps: Deps) -> None:
    last = st.get("_last_detail")
    if last is not None:
        wait = DETAIL_GAP - (deps.clock() - last)
        if wait > 0:
            deps.sleep(wait)
    st["_last_detail"] = deps.clock()


def paid_loop(st: dict, deps: Deps, key: str) -> None:
    """付费循环。每次发之前查上限（查过、记账、再发）；上一篇的下载放到下一次调用之前做，
    所以最后一次付费调用之后紧接着读余额（finally 里，出错也读），最后一篇的下载在读完余额之后。"""
    ledger, rule, plan = st["ledger"], K.StopRule(), st["plan"]
    pending = None
    try:
        for slot in K.CALL_ORDER:
            if slot == "F10":
                cand = K.flex_choice([c for c in st["calls"] if c["slot"] in ("C1", "C2", "C3", "C4", "C5", "C6")], plan)
            else:
                cand = plan["slots"].get(slot)
            if cand is None or not K.XID_RE.match(str(cand.get("xid") or "")):
                continue
            try:
                ledger.check_detail()
            except K.CapReached:
                st["cap_hit"] = True                      # 到上限是正常结束，不是停止规则
                break
            if pending is not None:
                _download(pending, st)
                pending = None
            ledger.charge_detail()                        # 记账在发之前：发出去失败也算钱，不重试
            _pace_detail(st, deps)
            word, raw = tikhub_detail(deps.fetch, key, cand["xid"])
            rec = _new_rec(slot, cand)
            rec["status"] = word
            st["calls"].append(rec)
            say(f"call {ledger.detail_calls}/{K.MAX_CALLS} {slot} {cand['pool']} {cand['stratum']}: {word}")
            if word == "ok":
                _analyze(rec, raw, cand, st)
            raw = None
            stop = rule.after(word, rec.get("path") == "absent" and not rec.get("is_video"))
            if "_dl" in rec:
                pending = rec
            if stop:
                st["stop"] = stop
                break
    finally:
        if st.get("balance_before") is not None:
            _balance_after(st, deps, key)
    if pending is not None:
        _download(pending, st)


def recheck(st: dict, deps: Deps, minutes: int) -> None:
    oks = [c for c in st["calls"] if (c.get("download") or {}).get("status") == "ok"]
    if not oks:
        return
    if minutes == 0:
        for c in oks:
            c["recheck"] = "skipped"
        return
    if (deps.clock() - st["t0"]) + minutes * 60 > K.RECHECK_BUDGET_MIN * 60:
        for c in oks:
            c["recheck"] = "skipped_time"
        return
    say(f"recheck in {minutes} min")
    deps.sleep(minutes * 60)
    for c in oks:
        try:
            word, same = st["dl"].recheck(c["slot"])
        except Exception as exc:  # noqa: BLE001
            word, same = "download_error", None
            c["analysis_error"] = c.get("analysis_error") or exc_word(exc)
        c["recheck"], c["recheck_same_bytes"] = word, same
        say(f"recheck {c['slot']}: {word}")


def _print_plan(sel: dict) -> None:
    t, c = sel["targets"], sel["controls"]
    say(f"selection source={sel['source']} hits={sel['hits']} hits_xhs={sel['hits_xhs']} "
        f"with_feishu_image={sel['with_feishu_image']} without_feishu_image={sel['without_feishu_image']} "
        f"(xhs {sel['without_feishu_image_xhs']})")
    say("targets " + " ".join(f"{k}={v}" for k, v in t.items()))
    say("controls " + " ".join(f"{k}={v}" for k, v in c.items()))
    say(f"guards drift={sel['drift']} window_now={sel['window_now']}")
    for s in sel["slot_labels"]:
        say(f"slot {s}")
    say(f"reserves control={sel['reserves']['control']} target={sel['reserves']['target']}")
    say(f"money planned_calls={sel['planned_calls']} max_calls={K.MAX_CALLS} "
        f"planned_yuan={round(sel['planned_calls'] * K.PRICE_YUAN, 3)} max_yuan={K.MAX_YUAN}")


def execute(mode: str, minutes: int, deps: Deps, st: dict) -> None:
    env = deps.env
    url, skey = env.get("SUPABASE_URL", "") or "", env.get("SUPABASE_SERVICE_ROLE_KEY", "") or ""
    _SECRETS.extend(s for s in (skey,) if s)
    if not (url and skey):
        raise K.SelectionFailed("missing_env")
    tv = read_tv(deps.fetch, url, skey)
    pools, funnel = K.build_pools(tv["notes"], tv["source"], tv["fi"], tv["excluded"], tv["vanished"])
    plan = K.plan_slots(pools)
    st["plan"] = plan
    st["eligible_targets"] = funnel["targets"]["eligible"]
    st["short_no_id"] = funnel["targets"]["short_no_id"]
    labels = [K.slot_label(s, plan["slots"].get(s)) for s in K.CALL_ORDER]
    planned = (sum(1 for s in K.CALL_ORDER if s != "F10" and plan["slots"].get(s) is not None)
               + (1 if plan["control_reserves"] or plan["target_reserves"] else 0))
    sel = dict(source=deps.source_word or tv["source_word"], **funnel, drift=K.drift_word(funnel),
               window_now="crawl_window" if K.in_crawl_window(deps.now()) else "open",
               slots=[{"slot": s, "pool": (plan["slots"].get(s) or {}).get("pool", "flex" if s == "F10" else "none"),
                       "stratum": (plan["slots"].get(s) or {}).get("stratum", "none")} for s in K.CALL_ORDER],
               reserves={"control": len(plan["control_reserves"]), "target": len(plan["target_reserves"])},
               planned_calls=planned)
    st["selection"] = sel
    sel_print = dict(sel, slot_labels=labels)
    _print_plan(sel_print)
    if mode == "plan":
        st["status"], st["reason"] = "planned", "planned"
        say("plan only: no TikHub, GitHub or image request was made")
        return
    key = (env.get("TIKHUB_API_KEY", "") or "").strip()
    if not key:
        raise Refused("refused_missing_key")
    _SECRETS.append(key)
    repo, token, sha = env.get("GITHUB_REPOSITORY", "") or "", env.get("GITHUB_TOKEN", "") or "", env.get("GITHUB_SHA", "") or ""
    if not (repo and token and sha):
        raise Refused("refused_missing_token")
    _SECRETS.append(token)
    if sel["window_now"] != "open":
        raise Refused("refused_window")
    if sel["drift"] != "ok":
        raise Refused("refused_drift")
    if funnel["controls"]["eligible"] < K.MIN_CONTROLS or funnel["targets"]["eligible"] < K.MIN_TARGETS:
        raise Refused("refused_pool")
    st["ledger"].charge_balance_read()
    word, bal = tikhub_balance(deps.fetch, key)
    if word != "ok":
        raise Refused(f"balance_{word}")
    if bal < K.BALANCE_FLOOR_USD:
        st["floor"] = "low"
        raise Refused("balance_low")
    st["floor"] = "ok"
    claim = claim_run(repo, token, sha, fetch=deps.fetch)
    if claim != "claimed":
        raise Refused(claim)
    st["balance_before"] = bal                          # 绝对值只在这个局部浮点里，不打印、不写
    say(f"claimed the single run; paid loop starts (max {K.MAX_CALLS} calls, max yuan {K.MAX_YUAN})")
    paid_loop(st, deps, key)
    if st.get("stop"):
        st["status"], st["reason"] = "stopped", st["stop"]
    else:
        st["status"], st["reason"] = "completed", ("cap_reached" if st.get("cap_hit") else "done")
    bal_line = _balance_section(st)
    say(f"balance diff_usd={bal_line['diff_usd']} diff_yuan={bal_line['diff_yuan']} expected_usd={bal_line['expected_usd']} "
        f"label=whole_account_shared")
    recheck(st, deps, minutes)


def _balance_section(st: dict) -> dict:
    before, after = st.get("balance_before"), st.get("balance_after")
    calls = st["ledger"].detail_calls
    out = {"diff_usd": None, "diff_yuan": None, "expected_usd": round(calls * K.USD_PER_CALL, 4), "excess_usd": None,
           "label": "whole_account_shared", "floor": st.get("floor", "na"),
           "after_read": st.get("balance_after_word", "na")}
    if before is not None and after is not None:
        diff = round(before - after, 4)
        out.update(diff_usd=diff, diff_yuan=round(diff * K.FX, 4), excess_usd=round(diff - out["expected_usd"], 4))
    return out


def _call_row(c: dict) -> dict:
    a, d = c.get("aspect"), c.get("download")
    return {"slot": c["slot"], "pool": c["pool"], "stratum": c["stratum"], "status": c["status"],
            "note_type": c["note_type"], "model_type": c["model_type"], "n_images": K.n_images_bucket(c.get("n_images")),
            "index_order": c["index_order"], "n_files_cmp": c["n_files_cmp"], "path": c["path"],
            "analysis_error": c.get("analysis_error"),
            "aspect": None if not a else {"category": a["category"], "d0": K.d_bucket(a.get("d0")),
                                          "best_index": a["best_index"], "dims_source": a["dims_source"],
                                          "origin_vs_item": a["origin_vs_item"], "discriminating": a["discriminating"],
                                          "size_match_index": c.get("size_match_index") or "na"},
            "download": None if not d else {k: d.get(k) for k in (
                "status", "tier", "probes", "scheme", "scheme_used", "ct_family", "format", "bytes", "url_key",
                "xhs_named", "on_known_xhs_domain", "bytes_vs_json_aspect", "video", "redirected", "reject",
                "not_attempted_reason")},
            "recheck": c.get("recheck", "na"), "recheck_same_bytes": c.get("recheck_same_bytes")}


def build_report(mode: str, st: dict, deps: Deps, minutes: int) -> dict:
    ledger = st["ledger"]
    rep = {"schema": K.SCHEMA, "mode": mode, "date_utc": deps.now().strftime("%Y-%m-%d"), "status": st["status"],
           "reason": st["reason"], "exception_class": st.get("exception_class"), "scrubbed": 0,
           "caps": {"max_calls": K.MAX_CALLS, "max_yuan": K.MAX_YUAN, "price_yuan": K.PRICE_YUAN,
                    "max_image_requests": K.MAX_IMAGE_REQUESTS, "max_balance_reads": K.MAX_BALANCE_READS},
           "ledger": {"detail_calls": ledger.detail_calls, "yuan": ledger.yuan, "balance_reads": ledger.balance_reads,
                      "image_requests": ledger.image_requests},
           "selection": st.get("selection"), "selection_error": st.get("selection_error"),
           "calls": [_call_row(c) for c in st["calls"]]}
    summ = K.summarize({"calls": st["calls"], "shape": st["shape"], "dl": st["dl"].summary(), "status": st["status"],
                        "recheck_minutes": minutes})
    rep.update(summ)
    rep["balance"] = _balance_section(st)
    n = st.get("eligible_targets")
    rep["projection"] = {"eligible_targets": n, "yuan_per_call": K.PRICE_YUAN,
                         "full_yuan": None if n is None else round(n * K.PRICE_YUAN, 2),
                         "short_no_id_not_covered": st.get("short_no_id")}
    rep["caveats"] = list(K.CAVEATS)
    clean, n_scrubbed = K.scrub(rep)
    clean["scrubbed"] = n_scrubbed
    return clean


# ── 报告 md（从清洗过的 JSON 生成） ──

def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, dict):
        return ", ".join(f"{k} {_fmt(x)}" for k, x in v.items()) or "–"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


def render_md(rep: dict, per_slot: bool = True) -> str:
    v, led, bal = rep.get("verdicts") or {}, rep["ledger"], rep.get("balance") or {}
    q3, q4 = rep.get("q3_download") or {}, rep.get("q4_targets") or {}
    L = [f"## 封面试点（I-35）· {rep['mode']} · {rep['status']} · {rep['reason']}", "",
         f"详情调用 {led['detail_calls']} 次 · ¥{led['yuan']} · 余额差（整个共用账户）{_fmt(bal.get('diff_usd'))} 美元"
         f" ≈ {_fmt(bal.get('diff_yuan'))} 元，本次应扣 {_fmt(bal.get('expected_usd'))} 美元 · 图片请求 {led['image_requests']} 次"
         f" · 清洗 {rep['scrubbed']} 处"
         + (f" · 出错 {rep['exception_class']}" if rep.get("exception_class") else ""), ""]
    if rep["mode"] != "plan":
        L += ["| 问题 | 结论 |", "|---|---|",
              f"| Q1 图片列表的形状 | {_fmt(v.get('q1'))} |",
              f"| Q2a 列表顺序 = 显示顺序（按 index） | {_fmt(v.get('q2_order'))} |",
              f"| Q2b 对照篇宽高比佐证（弱：只看宽高比） | {_fmt(v.get('q2_aspect'))} |",
              f"| Q2 第 0 项就是封面 | {_fmt(v.get('q2'))} |",
              f"| Q3 GitHub runner 直接下载 | {_fmt(v.get('q3'))}（要的请求头档位 {_fmt(q3.get('tier_needed'))}） |",
              f"| Q3 复查 · 链接里的过期类参数 | {_fmt(v.get('q3_recheck'))} · {_fmt(v.get('expiry_markers'))} |",
              f"| Q4 目标篇删了或隐藏的比例 | {_fmt(q4.get('gone_or_hidden_share'))} {_fmt(q4.get('wilson95'))}（n ≤ 4，只作参考） |",
              f"| 飞书图和小红书图的相似度比对 | {_fmt(v.get('approved_similarity_comparison'))} |",
              f"| 建议 | {_fmt(v.get('recommendation'))} · {_fmt(v.get('recommendation_reason'))} |", ""]
    sel = rep.get("selection")
    if sel:
        L += ["### 选样漏斗", "",
              f"来源 {sel.get('source')} · 爆帖 {sel.get('hits')}（小红书 {sel.get('hits_xhs')}）· 有飞书首图 {sel.get('with_feishu_image')}"
              f" · 没有 {sel.get('without_feishu_image')}（小红书 {sel.get('without_feishu_image_xhs')}）· 漂移 {sel.get('drift')}"
              f" · 现在 {sel.get('window_now')}", "",
              f"- 目标：{_fmt(sel.get('targets'))}", f"- 对照：{_fmt(sel.get('controls'))}",
              f"- 后备：{_fmt(sel.get('reserves'))} · 计划调用 {sel.get('planned_calls')}",
              "- 槽位：" + ", ".join(f"{s.get('slot')} {s.get('pool')} {s.get('stratum')}" for s in sel.get("slots") or []), ""]
    elif rep.get("selection_error"):
        L += [f"读 TV 失败：{rep['selection_error']}（一次付费调用都没发）", ""]
    if per_slot and rep.get("calls"):
        L += ["### 每个槽位（分档）", "", "| 槽位 | 池 | 层 | 状态 | 类型 | 图数 | 顺序 | 宽高比 / d0 / 最像的项 | 下载 / 档位 / 格式 / 大小 | 复查 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for c in rep["calls"]:
            a, d = c.get("aspect") or {}, c.get("download") or {}
            L.append(f"| {c['slot']} | {c['pool']} | {c['stratum']} | {c['status']} | {c['note_type']} | {c['n_images']} | "
                     f"{c['index_order']} | {_fmt(a.get('category'))} / {_fmt(a.get('d0'))} / {_fmt(a.get('best_index'))} | "
                     f"{_fmt(d.get('status'))} / {_fmt(d.get('tier'))} / {_fmt(d.get('format'))} / {_fmt(d.get('bytes'))} | "
                     f"{_fmt(c.get('recheck'))} |")
        L.append("")
    if rep["mode"] != "plan":
        q1, q2, asp = rep.get("q1_shape") or {}, rep.get("q2_order") or {}, rep.get("q2_aspect") or {}
        L += ["### Q1 键名（只有名字和计数）", "", f"- 路径：{_fmt(q1.get('path'))}", f"- 信封：{_fmt(q1.get('envelope_keys'))}",
              f"- data[0]：{_fmt(q1.get('data0_keys'))}", f"- 图片项：{_fmt(q1.get('item_keys'))}",
              f"- 像封面的键：{_fmt(q1.get('cover_like_keys'))}", f"- 不合格：{_fmt(q1.get('v1_failures'))}；怪键名 {q1.get('odd_keys')}", "",
              "### Q2", "", f"- 顺序：{_fmt(q2.get('counts'))}（≥2 张图的 {q2.get('eligible_posts')} 篇）",
              f"- 宽高比（容差 {asp.get('tolerance')}）：{_fmt(asp.get('by_category'))}；单图 {_fmt(asp.get('single'))}；"
              f"多图 {_fmt(asp.get('multi'))}；d0 {_fmt(asp.get('d0'))}；分得开的 {asp.get('discriminating')}",
              f"- 字节数对得上的项（探索性）：{_fmt((rep.get('q2_size') or {}).get('size_match_index'))}",
              f"- 视频：{_fmt(rep.get('video'))}", "",
              "### 下载与过期参数", "",
              f"- 状态：{_fmt(q3.get('status_counts'))}；格式 {_fmt(q3.get('format'))}；大小 {_fmt(q3.get('bytes'))}；"
              f"content-type {_fmt(q3.get('ct_family'))}；scheme {_fmt(q3.get('scheme'))}",
              f"- 主机 {q3.get('hosts_distinct')} 个、可注册域 {q3.get('registrable_distinct')} 个；名字像小红书的 {_fmt(q3.get('xhs_named'))}；"
              f"在已知域下的 {_fmt(q3.get('on_known_xhs_domain'))}",
              f"- 参数名：{_fmt(q3.get('param_names'))}；不合格参数 {q3.get('odd_params')}；过期类参数值的形状 {_fmt(q3.get('expiry_value_shapes'))}",
              f"- 复查（{q3.get('recheck_minutes')} 分钟后）：{_fmt(q3.get('recheck'))}；字节相同 {_fmt(q3.get('recheck_same_bytes'))}", "",
              "### Q4 目标篇", "", f"- 按链接：{_fmt(q4.get('by_link'))}", "",
              "### 全量预估", "", f"- {_fmt(rep.get('projection'))}", ""]
    L += ["### 固定说明", "",
          "- 第 0 项是第一张显示的图（按 index）；「第一张就是封面」是小红书的平台规则（推断）；宽高比证据弱，不比像素。",
          "- owner 11-18 同意的那种「飞书图和小红书图按相似度比」这次没做：Jev 拿不到飞书的图。",
          "- 对照篇只来自两个项目；「还活着」是 1–4 周前巡查的结果；余额差是整个共用账户的差；视频笔记占多少要跑了才知道；"
          "没有作品ID 的短链接不在范围里。", ""]
    return "\n".join(L)


def write_outputs(rep: dict, out: Path, summary: Optional[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT_JSON).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / REPORT_MD).write_text(render_md(rep, per_slot=True), encoding="utf-8")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(render_md(rep, per_slot=False) + "\n")


def main(argv: Optional[list] = None, deps: Optional[Deps] = None) -> int:
    ap = argparse.ArgumentParser(description="I-35 cover pilot (manual, one-off)")
    ap.add_argument("mode", choices=["plan", "rehearse", "run"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", default="")
    ap.add_argument("--recheck-minutes", type=int, choices=list(K.RECHECK_CHOICES), default=20)
    a = ap.parse_args(argv)
    restore = _harden()
    _SECRETS.clear()
    if deps is None:
        if a.mode == "rehearse":
            from scripts import cover_pilot_rehearse as R
            deps = R.world()
        else:
            deps = default_deps()
    ledger = K.Ledger()
    st = {"calls": [], "ledger": ledger, "shape": K.Shape(), "dl": Downloader(ledger, deps.fetch, deps.sleep, deps.clock),
          "status": "crashed", "reason": "exception", "t0": deps.clock()}
    code = 1
    try:
        try:
            execute(a.mode, a.recheck_minutes, deps, st)
        except K.SelectionFailed as exc:
            st["status"], st["reason"] = "refused", "selection_failed"
            word = str(exc.args[0]) if exc.args else "unknown"
            st["selection_error"] = word if K._ok_value(word) else "unknown"
        except Refused as exc:
            st["status"], st["reason"] = "refused", str(exc.args[0])
        except BaseException as exc:  # noqa: BLE001 — 只打类名：消息和 traceback 里可能有 URL
            st["status"], st["reason"], st["exception_class"] = "crashed", "exception", exc_word(exc)
            print(f"cover pilot crashed: {exc_word(exc)}", file=sys.stderr, flush=True)
        code = {"planned": 0, "completed": 0, "stopped": 3, "refused": 2}.get(st["status"], 1)
        if st["status"] == "refused":
            say(f"refused before any paid call: {st['reason']}")
    finally:
        try:
            rep = build_report(a.mode, st, deps, a.recheck_minutes)
            write_outputs(rep, Path(a.out), a.summary or None)
            v = rep.get("verdicts") or {}
            say(f"status={rep['status']} reason={rep['reason']} calls={rep['ledger']['detail_calls']} yuan={rep['ledger']['yuan']}")
            if a.mode != "plan":
                say("verdicts " + " ".join(f"{k}={v.get(k)}" for k in ("q1", "q2_order", "q2_aspect", "q2", "q3",
                                                                       "q3_recheck", "expiry_markers", "recommendation",
                                                                       "recommendation_reason")))
        except BaseException as exc:  # noqa: BLE001
            print(f"cover pilot report failed: {exc_word(exc)}", file=sys.stderr, flush=True)
            code = 1
        finally:
            _SECRETS.clear()
            restore()
    return code


if __name__ == "__main__":
    sys.exit(main())
