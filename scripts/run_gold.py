#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金标准评测：题库 + 用例 → Jev（或 mock）→ 歧义判定 → 与金标准比对 → Markdown 报告。

  python3 scripts/run_gold.py banks/comment_reader_v0.3.yaml fixtures/comment_cases.json \
      --gold banks/gold/comment_reader_gold_v0.1.yaml --out report.md [--raw raw.json] [--mock] [--lang en]

用例文件：{"说明": "...", "shared": {...}, "fill": {占位符: 值}, "cases": [{"id": "c1", "state": {...}, "note": "..."}]}
  fill 填题库题干里按篇变的占位符（评论题库 v0.4 的帖子要点、主体称呼）；题库没有占位符时省略。
金标准：cases[].expect.<题号> 选择题写可接受选项列表；是非题写 true / false / 歧义；<题号>_ambiguous: true = 期望标歧义。
退出码：0 完成；2 输入错误；3 完成但有不中；4 没密钥。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import check_bank, load_bank, unfilled  # noqa: E402
from judge.core import judge_state  # noqa: E402
from judge.jev_client import JevClient, JevError  # noqa: E402


def compare(items: dict, expect: dict) -> dict:
    out = {}
    for qid, it in items.items():
        if qid not in expect and f"{qid}_ambiguous" not in expect:
            out[qid] = ("n/a", "金标准没标"); continue
        exp = expect.get(qid)
        want_amb = bool(expect.get(f"{qid}_ambiguous")) or exp == "歧义"
        if want_amb:
            ok = it["ambiguous"]
            out[qid] = ("hit" if ok else "miss", "期望歧义，" + ("标了" if ok else "没标")); continue
        if "p_yes" in it:
            ok = bool(it["raw"]) == bool(exp)
            note = f"期望 {'是' if exp else '否'}"
        else:
            acceptable = exp if isinstance(exp, list) else [exp]
            ok = it["answer"] in acceptable
            note = f"期望 {'/'.join(map(str, acceptable))}"
        if ok and it["ambiguous"]:
            note += "（答对但标了歧义）"
        out[qid] = ("hit" if ok else "miss", note)
    return out


def fmt(it: dict) -> str:
    s = f"{it['answer']} {it['p']:.2f}"
    return s + ("  ⚠歧义" if it["ambiguous"] else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bank"); ap.add_argument("cases")
    ap.add_argument("--gold"); ap.add_argument("--out"); ap.add_argument("--raw")
    ap.add_argument("--mock", action="store_true"); ap.add_argument("--lang", default="zh")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    try:
        bank = load_bank(args.bank, lang=args.lang)
        problems = check_bank(bank)
        if problems:
            print("题库有问题：" + "；".join(problems), file=sys.stderr); return 2
        cases_file = json.loads(Path(args.cases).read_text(encoding="utf-8"))
        gold = {c["id"]: c for c in (yaml.safe_load(Path(args.gold).read_text(encoding="utf-8")) or {}).get("cases", [])} if args.gold else {}
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr); return 2
    try:
        client = JevClient(mock=args.mock)
    except JevError as exc:
        print(str(exc), file=sys.stderr); return 4

    def state_of(case):
        st = {}
        if cases_file.get("说明"):
            st["说明"] = cases_file["说明"]
        st.update(cases_file.get("shared") or {}); st.update(case["state"]); return st

    fill = cases_file.get("fill") or {}
    left = unfilled(bank, fill)
    if left:
        print(f"题库 {bank.name} 的占位符没填全：{left}（在用例文件的 fill 里给）", file=sys.stderr); return 2

    def run_one(case):
        r = judge_state(client, bank, case["id"], state_of(case), fill=fill)
        return case, r

    rows = []
    try:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            rows = list(ex.map(run_one, cases_file["cases"]))
    except JevError as exc:
        print(f"调用失败：{exc}", file=sys.stderr); return 1

    qids = bank.ids()
    hits = {q: [0, 0] for q in qids}
    lines = [f"# 金标准评测 · {bank.name} {bank.version} · {bank.model} · {'mock' if args.mock else 'live'} · 题干 {args.lang}", ""]
    if args.mock:
        lines += ["> mock：答案是假的，只看流程。", ""]
    misses = 0
    for case, r in rows:
        exp = (gold.get(case["id"]) or {}).get("expect") or {}
        cmp_ = compare(r.items, exp) if exp else {}
        lines += [f"## {case['id']}", f"> {case['state'].get('评论原文') or json.dumps(case['state'], ensure_ascii=False)[:120]}", "",
                  "| 题 | Jev | 金标准 | 判 |", "|---|---|---|---|"]
        for q in qids:
            it = r.items.get(q)
            if not it:
                continue
            v, note = cmp_.get(q, ("n/a", ""))
            if v != "n/a":
                hits[q][1] += 1; hits[q][0] += v == "hit"; misses += v == "miss"
            lines.append(f"| {q} | {fmt(it)} | {note} | {'✓' if v == 'hit' else '✗' if v == 'miss' else '–'} |")
        if (gold.get(case["id"]) or {}).get("why"):
            lines += ["", f"金标准理由：{gold[case['id']]['why']}"]
        lines.append("")
    if gold:
        lines += ["## 按题汇总", "", "| 题 | 命中 |", "|---|---|"]
        for q in qids:
            h, t = hits[q]; lines.append(f"| {q} | {h}/{t} |" if t else f"| {q} | – |")
        th = sum(h for h, _ in hits.values()); tt = sum(t for _, t in hits.values())
        lines += ["", f"合计 {th}/{tt}", ""]
    report = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8"); print(f"报告 → {args.out}")
    else:
        print(report)
    if args.raw:
        Path(args.raw).write_text(json.dumps([{"case": c, "items": r.items, "usage": r.usage} for c, r in rows],
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    return 3 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
