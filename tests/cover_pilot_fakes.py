# -*- coding: utf-8 -*-
"""cover_pilot 测试用的假世界（不是 pytest 用例文件）：假 TV（PostgREST）、假 TikHub、假 GitHub、假图片服务器，全部离线。
主机都在 .invalid 下（TikHub / GitHub 的主机名是代码里写死的常量，这里只是按它们分流，不联网）。"""
from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from scripts import cover_pilot as P

WED = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
SUPA = "https://projref.supabase.invalid"
IMG = "img.xhsfake.invalid"


def xid_of(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:24]


def webp(w: int, h: int) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body + b"\x00" * 32


def png(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


def note_row(nid, *, tier="爆", platform="xiaohongshu", url="", wt=None, wu=None, pt="正常", pu=None, at=True, au=None,
             project="PA"):
    return {"note_id": nid, "project_id": project, "platform": platform, "tier": tier, "publish_url": url, "wt": wt,
            "wu": wu, "pt": pt, "pu": pu, "at": at, "au": au}


def tables(n_single=5, n_multi=7, n_long=300, n_short_id=5, n_short_no=5, tall_every=2, prefix="n"):
    """默认：12 个对照（5 单图 / 7 多图，答过、有宽高、活着），310 篇没有飞书首图（300 长链 + 5 短链带作品ID + 5 短链没有）。"""
    notes, source, fi, roles = [], [], [], {}
    for i in range(n_single + n_multi):
        nid = f"{prefix}_ctl_{i:02d}"
        xid = xid_of(nid)
        single = i < n_single
        tall = i % tall_every == 0
        w, h = (1080, 1920) if tall else (900, 1200)
        link = f"https://xhslink.invalid/c{i}"
        notes.append(note_row(nid, url=link, wu=f"{xid} ← {link}"))
        sha = hashlib.sha256(nid.encode()).hexdigest()
        n_files = 1 if single else 3
        source.append({"note_id": nid, "n_files": n_files, "file_sha": sha})
        fi.append({"note_id": nid, "width": w // 2, "height": h // 2, "status": "answered", "n_files": n_files,
                   "file_sha": sha, "file_kind": "jpeg", "file_bytes": 300_000 + i})
        roles[xid] = {"pool": "control", "w": w, "h": h, "n": 2 if single else 3, "fs_bytes": 300_000 + i}
    for i in range(n_long + n_short_id + n_short_no):
        nid = f"{prefix}_tgt_{i:03d}"
        xid = xid_of(nid)
        if i < n_long:
            link, wu = f"https://www.xiaohongshu.com/explore/{xid}", None
        elif i < n_long + n_short_id:
            link = f"https://xhslink.invalid/t{i}"
            wu = f"{xid} ← {link}"
        else:
            link, wu = f"https://xhslink.invalid/n{i}", None
        notes.append(note_row(nid, url=link, wu=wu, pt=None, at=None, project="PC"))
        roles[xid] = {"pool": "target", "w": 900, "h": 1200, "n": 2, "fs_bytes": None}
    return {"notes": notes, "v_note_first_image_source": source, "note_first_image": fi,
            "first_image_bank_state": [{"excluded_projects": []}], "v_notes_vanished": [], "_roles": roles}


def item(i, w, h, url=None, size=None):
    url = url or f"https://{IMG}/w{w}h{h}i{i}?sign=abcd1234&t=6735a8f0&ap=1&sc=2&imageView2/2/w/1080/format/webp"
    return {"index": i, "fileid": "fid", "url": url, "url_size_large": url, "original": url,
            "url_multi_level": {"low": url}, "width": w, "height": h,
            "origin_img": {"width": w, "height": h, "size": size if size is not None else 1000 + i}}


def ok_body(xid, items, *, note_extra=None, envelope_extra=None):
    note = {"id": xid, "type": "normal", "model_type": "note", "title": "t", "desc": "d", "images_list": items}
    note.update(note_extra or {})
    env = {"code": 200, "router": "/r", "params": {"note_id": xid}, "cache_url": "https://cache.invalid/c",
           "data": {"code": 0, "success": True, "data": [{"note_list": [note], "model_type": "note", "user": {}}]}}
    env.update(envelope_extra or {})
    return env


GONE = {"code": 200, "data": {"code": 0, "success": True, "data": []}}
HIDDEN = {"code": 200, "data": {"code": 0, "success": True, "data": [{"note_list": [{"model_type": "error", "image": "x"}]}]}}
UPSTREAM = {"code": 200, "data": {"code": 500, "success": False, "data": []}}


class World:
    """detail(xid, k, role) → dict（200 JSON）| P.Resp | 抛异常；image(url, headers, k) → P.Resp | 抛异常。k 从 1 数。"""

    def __init__(self, t=None, *, detail=None, image=None, balances=(10.0, 9.9), claim=201, now=WED, env=None, hook=None):
        self.t = t or tables()
        self.roles = self.t.pop("_roles", {})
        self.detail_fn, self.image_fn = detail, image
        self.balances = list(balances)
        self.claim_status = claim
        self.now = now
        self.log: list = []
        self.clock_v = [5000.0]
        self.sleeps: list = []
        self.hook = hook
        self.env = env if env is not None else {
            "SUPABASE_URL": SUPA, "SUPABASE_SERVICE_ROLE_KEY": "svc-key-x", "TIKHUB_API_KEY": "tikhub-key-x",
            "GITHUB_TOKEN": "gh-token-x", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40}

    def kinds(self, kind):
        return [x for x in self.log if x[0] == kind]

    def clock(self):
        self.clock_v[0] += 0.01
        return self.clock_v[0]

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock_v[0] += max(0.0, float(s))

    def deps(self):
        return P.Deps(fetch=self.fetch, sleep=self.sleep, clock=self.clock, now=lambda: self.now, env=self.env)

    def default_detail(self, xid, k, role):
        if role is None:
            return GONE
        items = [item(0, role["w"], role["h"], size=role["fs_bytes"])] + [item(i, 900, 1200) for i in range(1, role["n"])]
        return ok_body(xid, items)

    def default_image(self, url, headers, k):
        p = urlsplit(url)
        import re
        m = re.match(r"/w(\d+)h(\d+)", p.path)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (900, 1200)
        return P.Resp(200, {"content-type": "image/webp"}, webp(w, h), "")

    def fetch(self, url, headers, *, timeout, cap, method="GET", data=None, stop_ct=None):
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if self.hook:
            self.hook(self, url)
        if host == urlsplit(self.env.get("SUPABASE_URL") or SUPA).hostname:
            self.log.append(("supabase", method, data, url))
            path = unquote(p.path).split("/rest/v1/", 1)[1]
            q = parse_qs(unquote(p.query))
            rows = self.t[path]
            if callable(rows):
                return rows(url)
            off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["1000"])[0])
            return P.Resp(200, {}, json.dumps(rows[off:off + lim], ensure_ascii=False).encode(), "")
        if host == "api.tikhub.io":
            if p.path.endswith("/get_user_info"):
                self.log.append(("balance",))
                b = self.balances.pop(0) if self.balances else 9.0
                if isinstance(b, (P.Resp,)):
                    return b
                if isinstance(b, BaseException):
                    raise b
                return P.Resp(200, {}, json.dumps({"code": 200, "user_data": {"balance": b, "account_disabled": False}}).encode(), "")
            xid = parse_qs(p.query)["note_id"][0]
            k = len(self.kinds("detail")) + 1
            self.log.append(("detail", xid, headers))
            role = self.roles.get(xid)
            out = (self.detail_fn or self.default_detail)(xid, k, role)
            if isinstance(out, P.Resp):
                return out
            return P.Resp(200, {}, json.dumps(out, ensure_ascii=False).encode(), "")
        if host == "api.github.com":
            self.log.append(("claim", method, json.loads(data.decode()), url))
            return P.Resp(self.claim_status, {}, b"{}", "")
        k = len(self.kinds("image")) + 1
        self.log.append(("image", url, dict(headers)))
        out = (self.image_fn or self.default_image)(url, headers, k)
        if stop_ct is not None and isinstance(out, P.Resp) and stop_ct(out.headers.get("content-type")):
            return P.Resp(out.status, out.headers, b"", "ct_stopped")
        return out


def run(world, mode="run", tmp=None, recheck="0", extra=()):
    out = tmp / "out"
    summary = tmp / "summary.md"
    code = P.main([mode, "--out", str(out), "--summary", str(summary), "--recheck-minutes", recheck, *extra], deps=world.deps())
    rep = json.loads((out / P.REPORT_JSON).read_text(encoding="utf-8"))
    return code, rep
