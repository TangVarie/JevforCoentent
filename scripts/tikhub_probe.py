#!/usr/bin/env python3
"""TEMP 诊断 (11-27, 用完即撤), 第二轮: 第一轮 7 种参数组合 (含文档默认示例) 全是 HTTP 400 "Request failed"。
这一轮看: key 本身、TikHub 对这个端点的说法、另一个地址、小红书别的端点。只打印状态、脱敏的 code / message、
返回里有哪些键 / 多少条; 不打印账户字段的值和任何笔记内容。成功的请求按 0.01 美元算, 最多 9 次。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge import external as E  # noqa: E402

KEY = os.environ.get("TIKHUB_API_KEY", "")
SAFE_STATUS_KEYS = ("status", "state", "active", "enable", "maint", "available", "online", "deprecat", "message", "msg", "note")


def req(base: str, path: str, params: dict) -> tuple:
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json", "User-Agent": E.USER_AGENT})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(8192) if exc.fp is not None else b""
        except Exception:  # noqa: BLE001
            body = b""
        return exc.code, E._http_error_excerpt(body or b"", KEY)
    except Exception as exc:  # noqa: BLE001 — 只要类名, 不要带网址的异常文本
        return None, type(exc).__name__


def shape(obj, depth: int = 0) -> str:
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{k}:{shape(v, depth + 1) if depth < 1 else type(v).__name__}" for k in list(obj)[:12] for v in [obj[k]]) + "}"
    if isinstance(obj, list):
        return f"list[{len(obj)}]"
    return type(obj).__name__


def show(name: str, status, body) -> None:
    if isinstance(body, dict):
        top = {k: body.get(k) for k in ("code",) if k in body}
        data = body.get("data")
        print(f"[{name}] HTTP {status} · {top} · data={shape(data)} · 认出笔记 {len(E._find_notes(body))} 条")
    else:
        print(f"[{name}] HTTP {status} · {body}")


def main() -> int:
    io, dev = "https://api.tikhub.io", "https://api.tikhub.dev"
    sp = "/api/v1/xiaohongshu/app_v2/search_notes"
    # 1. TikHub 自己怎么说这个端点 (只印像状态的字段的值)
    st, body = req(io, "/api/v1/tikhub/user/get_endpoint_info", {"endpoint": sp})
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        d = body["data"]
        flat = {}
        def walk(o, pre=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{pre}{k}.")
            elif any(s in pre.lower() for s in SAFE_STATUS_KEYS):
                flat[pre[:-1]] = o if not isinstance(o, str) else o[:160]
        walk(d)
        print(f"[endpoint_info] HTTP {st} · 键: {sorted(d)[:20]} · 状态类字段: {flat}")
    else:
        show("endpoint_info", st, body)
    # 2. key 本身 (只看能不能用, 不印任何值)
    st, body = req(io, "/api/v1/tikhub/user/get_user_info", {})
    print(f"[user_info] HTTP {st} · " + (f"data 键数 {len(body.get('data') or {})}" if isinstance(body, dict) else str(body)))
    demo = {"keyword": "美食推荐", "page": 1, "sort_type": "general"}
    # 3. 默认示例, 照 TikHub 说的重试 3 次
    for i in range(3):
        st, body = req(io, sp, demo)
        show(f"search demo io try{i + 1}", st, body)
        if st == 200:
            break
        time.sleep(3)
    # 4. 另一个地址
    st, body = req(dev, sp, demo)
    show("search demo dev", st, body)
    # 5. 小红书别的端点
    st, body = req(io, "/api/v1/xiaohongshu/app_v2/search_users", {"keyword": "美食"})
    show("app_v2 search_users", st, body)
    st, body = req(io, "/api/v1/xiaohongshu/web_v3/fetch_search_suggest", {"keyword": "美食"})
    show("web_v3 search_suggest", st, body)
    st, body = req(io, "/api/v1/xiaohongshu/app_v2/get_creator_hot_inspiration_feed", {})
    show("app_v2 hot_inspiration", st, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
