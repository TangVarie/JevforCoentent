#!/usr/bin/env python3
"""TEMP 诊断 (11-27, 用完即撤): TikHub app_v2/search_notes 在 external-corpus 里 11-26 起全 400, owner 那边同一接口能用。
逐个参数组合各发 1 次第一页搜索, 只打印状态、脱敏后的 code / message、认出几条笔记。不打印笔记内容。
硬上限 0.10 美元 (成功的请求 0.01 美元一次; TikHub 说失败的请求不扣费)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge import external as E  # noqa: E402

COMBOS = [
    ("doc_demo", {"keyword": "美食推荐", "page": 1, "sort_type": "general"}),
    ("kw_page_only", {"keyword": "戒烟", "page": 1}),
    ("general", {"keyword": "戒烟", "page": 1, "sort_type": "general"}),
    ("general+普通笔记 (ours)", {"keyword": "戒烟", "page": 1, "sort_type": "general", "note_type": "普通笔记"}),
    ("popularity+普通笔记 (ours)", {"keyword": "戒烟", "page": 1, "sort_type": "popularity_descending", "note_type": "普通笔记"}),
    ("popularity", {"keyword": "戒烟", "page": 1, "sort_type": "popularity_descending"}),
    ("general+不限", {"keyword": "戒烟", "page": 1, "sort_type": "general", "note_type": "不限"}),
]


def main() -> int:
    client = E.TikHubClient(budget=E.Budget(limit_usd=0.10), rps=2)
    print(f"base={E.TIKHUB_BASE} path={E.SEARCH_PATH}")
    for name, params in COMBOS:
        shown = {k: v for k, v in params.items()}
        try:
            raw = client._get(E.SEARCH_PATH, params)
        except E.BudgetExceeded:
            print(f"[{name}] 预算到了, 停"); break
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] {shown} -> {type(exc).__name__}: {exc}")
            continue
        notes = E._find_notes(raw)
        top = raw.get("code") if isinstance(raw, dict) else None
        print(f"[{name}] {shown} -> HTTP 200 · code={top} · 认出 {len(notes)} 条笔记")
    print(f"共 {client.budget.calls} 次请求 · 按 0.01 美元计最多 {client.budget.spent:.2f} 美元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
