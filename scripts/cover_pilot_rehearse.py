# -*- coding: utf-8 -*-
"""cover_pilot.py rehearse 的假世界：TV（PostgREST）、TikHub 详情和余额、GitHub refs、图片服务器全在进程里，数据全是合成的。

用途：一次性的付费运行之前，在 runner 上把整条路径（槽位、记账、停止规则、下载档位、复查、报告、step summary、产物、退出码）
空跑一遍，不读任何密钥、不发任何网络请求、不用掉认领 tag。主机名都在 .invalid 下，id 是 md5 造的。
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

NOW = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)      # 周三晚上：不在 external-corpus 的抓取时段
BASE = "https://rehearse.invalid"
IMG_HOST = "img-rehearse.xhsfake.invalid"
SHORT = "https://xhslink.invalid/r"
Resp = namedtuple("Resp", "status headers body flag")


def _xid(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:24]


def _webp(w: int, h: int) -> bytes:
    """够 sniff / dims_from_bytes 认的 VP8X 头（不是能解码的图；试点从不解码）。"""
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body + b"\x00" * 64


def _tables() -> dict:
    notes, source, fi, vanished, roles = [], [], [], [], {}
    for i in range(14):
        nid = f"rehearse_ctl_{i:02d}"
        xid = _xid(nid)
        single = i < 6
        tall = i % 3 == 0
        w, h = (1080, 1920) if tall else (900, 1200)
        link = f"{SHORT}{i}"
        notes.append({"note_id": nid, "project_id": "REHEARSE_A" if i < 11 else "REHEARSE_B", "platform": "xiaohongshu",
                      "tier": "爆", "publish_url": link, "wt": None, "wu": f"{xid} ← {link}", "pt": "正常", "pu": None,
                      "at": True, "au": None})
        sha = hashlib.sha256(nid.encode()).hexdigest()
        source.append({"note_id": nid, "n_files": 1 if single else 3, "file_sha": sha})
        fi.append({"note_id": nid, "width": w // 2, "height": h // 2, "status": "answered" if i < 12 else "not_a_cover",
                   "n_files": 1 if single else 3, "file_sha": sha, "file_kind": "jpeg", "file_bytes": 200_000 + i})
        roles[xid] = ("control", w, h, 2 if single else 3)
    for i in range(310):
        nid = f"rehearse_tgt_{i:03d}"
        xid = _xid(nid)
        if i < 280:
            link, wu = f"https://www.xiaohongshu.com/explore/{xid}?xsec_token=REHEARSE", None
        elif i < 290:
            link, wu = f"https://www.xiaohongshu.com/discovery/item/{xid}", None
        elif i < 300:
            link = f"{SHORT}t{i}"
            wu = f"{xid} ← {link}"
        else:
            link, wu = f"{SHORT}n{i}", None
        notes.append({"note_id": nid, "project_id": "REHEARSE_C", "platform": "xiaohongshu", "tier": "大爆" if i % 7 == 0 else "爆",
                      "publish_url": link, "wt": None, "wu": wu, "pt": None, "pu": None, "at": None, "au": None})
        if i in (5, 291):
            vanished.append({"note_id": nid})
        roles[xid] = ("target", 900, 1200, 1 + i % 4)
    return {"notes": notes, "v_note_first_image_source": source, "note_first_image": fi,
            "first_image_bank_state": [{"excluded_projects": []}], "v_notes_vanished": vanished, "_roles": roles}


def _detail(xid: str, roles: dict) -> dict:
    role = roles.get(xid)
    if role is None:
        return {"code": 200, "data": {"code": 0, "success": True, "data": []}}
    pool, w, h, n = role
    if pool == "target":
        k = int(xid[:4], 16) % 3
        if k == 0:
            return {"code": 200, "data": {"code": 0, "success": True, "data": []}}
        if k == 1:
            return {"code": 200, "data": {"code": 0, "success": True,
                                          "data": [{"note_list": [{"model_type": "error", "image": "x"}]}]}}
    items = []
    for i in range(n):
        iw, ih = (w, h) if i == 0 else (900, 1200)
        url = f"https://{IMG_HOST}/w{iw}h{ih}i{i}?sign=abc&t=6735a8f0&ap=1&sc=2&imageView2/2/w/1080/format/webp"
        items.append({"index": i, "fileid": "rehearse", "url": url, "url_size_large": url, "original": url,
                      "url_multi_level": {"low": url}, "width": iw, "height": ih,
                      "origin_img": {"width": iw, "height": ih, "size": 1000 + i}})
    note = {"id": xid, "type": "normal", "model_type": "note", "title": "rehearse", "desc": "rehearse",
            "images_list": items, "user": {"nickname": "rehearse"}}
    return {"code": 200, "router": "/rehearse", "params": {"note_id": xid}, "cache_url": "https://cache.rehearse.invalid/c",
            "data": {"code": 0, "success": True, "data": [{"note_list": [note], "model_type": "note", "user": {}}]}}


def world():
    """返回 cover_pilot.Deps：假传输、假时钟（sleep 只往前拨）、固定的「现在」、合成的环境变量。"""
    from scripts.cover_pilot import Deps
    t = _tables()
    roles = t.pop("_roles")
    clock = [1000.0]
    detail_calls = [0]

    def tick() -> float:
        clock[0] += 0.1
        return clock[0]

    def sleep(s: float) -> None:
        clock[0] += max(0.0, float(s))

    def fetch(url, headers, *, timeout, cap, method="GET", data=None, stop_ct=None):
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if host == "rehearse.invalid":
            assert method == "GET" and data is None
            path = unquote(p.path).split("/rest/v1/", 1)[1]
            q = parse_qs(unquote(p.query))
            rows = t[path]
            off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["1000"])[0])
            return Resp(200, {"content-type": "application/json"}, json.dumps(rows[off:off + lim]).encode(), "")
        if host == "api.tikhub.io":
            if p.path.endswith("/get_user_info"):
                return Resp(200, {}, json.dumps({"code": 200, "user_data": {"balance": 10.0 - 0.01 * detail_calls[0],
                                                                          "account_disabled": False}}).encode(), "")
            detail_calls[0] += 1
            return Resp(200, {}, json.dumps(_detail(parse_qs(p.query)["note_id"][0], roles)).encode(), "")
        if host == "api.github.com":
            assert method == "POST"
            return Resp(201, {}, b"{}", "")
        if host == IMG_HOST:
            if "User-Agent" not in headers:
                return Resp(403, {"content-type": "text/html"}, b"", "")
            m = re.match(r"/w(\d+)h(\d+)i\d+$", p.path)
            return Resp(200, {"content-type": "image/webp"}, _webp(int(m.group(1)), int(m.group(2))), "")
        raise RuntimeError("rehearse: unexpected host")

    env = {"SUPABASE_URL": BASE, "SUPABASE_SERVICE_ROLE_KEY": "rehearse-service-key", "TIKHUB_API_KEY": "rehearse-tikhub-key",
           "GITHUB_TOKEN": "rehearse-token", "GITHUB_REPOSITORY": "rehearse/rehearse", "GITHUB_SHA": "0" * 40}
    return Deps(fetch=fetch, sleep=sleep, clock=tick, now=lambda: NOW, env=env, source_word="rehearse_fake")
