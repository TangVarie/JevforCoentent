#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""特征层影子跑：用 Jev 跑 TV 的 fq 题库（20 题 + 证据选句），与现行抽取器（Opus）和 D-081 的 Jev 表比对，并生成落库 SQL。

  # 从本地 JSON / TSV（fixtures 里有 gate1 的 50 篇）
  python3 scripts/fq_shadow.py --notes fixtures/fq_notes_gate1_50.json --opus fixtures/fq_opus_gate1_50.tsv \
      --gate1 fixtures/fq_jev_d081_A_gate1_50.tsv --out docs/fq-shadow.md --raw raw.json --sql shadow.sql --run-tag shadow-2026-09-23

  # 直接从库里取（需要 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY；只读 notes / note_feature_answers）
  python3 scripts/fq_shadow.py --from-db --limit 200 --mappings ../truth-vault/mappings [--project NUC_phase1] ...

切标题的方式按项目走 TV 的 mapping（title_extraction：markers / column / none，缺省 none，同 annotate_feature_pass），
所以 --from-db 必须给 --mappings（TV 仓的 mappings 目录）；不按 mapping 切，一致率会被切法差异污染（D-081 说的「切段差异」）。
本地文件模式仍用 --title-extraction 一个值。
现行抽取器的答案取 extractor like llm:* 的 primary 行；同一篇同一题有多个 llm 模型时取 extracted_at 最新的那条，报告里列出用到的 extractor。

Opus/D-081 的 TSV 格式：subject_id<TAB>q=答案[!无效原因][⟨证据⟩][~概率];...（与 note_feature_answers 一一对应）。
run_tag 用 shadow-*，不进 primary（docs/28：只有 primary 进分析）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import load_bank  # noqa: E402
from judge.core import judge_note, ledger_rows, result_to_dict, rows_to_sql  # noqa: E402
from judge.jev_client import JevClient, JevError  # noqa: E402
from judge.spans import norm  # noqa: E402

BANK = Path(__file__).resolve().parent.parent / "banks" / "vendor" / "feature_questions_v0_1.yaml"
_ITEM = re.compile(r"^([^!⟨~]*)(?:!([^⟨~]*))?(?:⟨(.*?)⟩)?(?:~([\d.]+))?$")


def parse_tsv(path):
    if not path:
        return {}
    res = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sid, a = line.split("\t", 1)
        d = {}
        for item in a.split(";"):
            if "=" not in item:
                continue
            qid, rest = item.split("=", 1)
            m = _ITEM.match(rest)
            ans, inv, ev, prob = m.groups() if m else (rest, None, None, None)
            d[qid] = {"answer": None if ans in ("∅", "") else ans, "invalid": inv, "evidence": ev,
                      "prob": float(prob) if prob else None}
        res[sid] = d
    return res


def overlap(short: str, long_: str) -> float:
    if len(short) < 4:
        return 1.0 if short and short in long_ else 0.0
    grams = [short[i:i + 4] for i in range(len(short) - 3)]
    return sum(1 for g in grams if g in long_) / len(grams)


def _pg(url, key, path):
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "truth_vault"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def load_mappings(dir_: str) -> dict:
    """TV 仓 mappings/*.yaml → {project_id: title_extraction}。project_id 缺省用文件名。"""
    import yaml
    out = {}
    for p in sorted(Path(dir_).glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        m = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out[str(m.get("project_id") or p.stem)] = m.get("title_extraction") or "none"
    if not out:
        sys.exit(f"--mappings {dir_} 里没有 mapping 文件")
    return out


def fetch_from_db(limit: int, project: str | None):
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("--from-db 需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    q = f"notes?select=note_id,project_id,title,raw_content&order=note_id&limit={limit}"
    if project:
        q += f"&project_id=eq.{urllib.parse.quote(project)}"
    notes = _pg(url, key, q)
    ids = ",".join(urllib.parse.quote(n["note_id"]) for n in notes)
    ans = _pg(url, key, f"note_feature_answers?select=subject_id,question_id,answer,evidence,invalid_reason,extractor,extracted_at"
                        f"&extractor=like.llm:*&run_tag=eq.primary&subject_id=in.({ids})")
    return notes, latest_per_cell(ans)


def latest_per_cell(ans: list) -> dict:
    """同一篇同一题有多个 llm:* 模型的 primary 行时取 extracted_at 最新的一条（以前是返回顺序后写覆盖先写）。"""
    best: dict = {}
    for a in ans:
        k = (a["subject_id"], a["question_id"])
        if k not in best or str(a.get("extracted_at") or "") > str(best[k].get("extracted_at") or ""):
            best[k] = a
    opus = defaultdict(dict)
    for (sid, qid), a in best.items():
        opus[sid][qid] = {"answer": a["answer"], "evidence": a["evidence"], "invalid": a["invalid_reason"], "extractor": a.get("extractor")}
    return dict(opus)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes"); ap.add_argument("--opus"); ap.add_argument("--gate1")
    ap.add_argument("--from-db", action="store_true"); ap.add_argument("--limit", type=int, default=100); ap.add_argument("--project")
    ap.add_argument("--title-extraction", default="markers", help="本地文件模式用；--from-db 按 --mappings 每个项目各自的切法")
    ap.add_argument("--mappings", help="TV 仓的 mappings 目录（--from-db 必填）")
    ap.add_argument("--out", default="fq-shadow.md"); ap.add_argument("--raw"); ap.add_argument("--sql")
    ap.add_argument("--run-tag", default="shadow"); ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--mock", action="store_true"); ap.add_argument("--from-raw")
    args = ap.parse_args()

    bank = load_bank(BANK, name="feature_questions_v0_1")
    modes: dict = {}
    if args.from_db:
        if not args.mappings:
            sys.exit("--from-db 要给 --mappings（TV 仓的 mappings 目录）：切标题按项目的 title_extraction 走，同 TV")
        modes = load_mappings(args.mappings)
        notes, opus = fetch_from_db(args.limit, args.project)
        unknown = sorted({n.get("project_id") for n in notes} - set(modes))
        if unknown:
            sys.exit(f"这些项目在 {args.mappings} 里没有 mapping：{unknown}")
    else:
        notes = json.loads(Path(args.notes).read_text(encoding="utf-8"))
        opus = parse_tsv(args.opus)
    gate1 = parse_tsv(args.gate1)

    if args.from_raw:
        results = json.loads(Path(args.from_raw).read_text(encoding="utf-8"))
    else:
        try:
            client = JevClient(mock=args.mock)
        except JevError as exc:
            sys.exit(str(exc))
        def one(n):
            mode = modes.get(n.get("project_id"), args.title_extraction) if modes else args.title_extraction
            r = judge_note(client, bank, n["note_id"], n.get("raw_content") or "", title_extraction=mode,
                           title_col=n.get("title"))
            d = result_to_dict(r); d["project_id"] = n.get("project_id"); return d
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(one, notes))
        if args.raw:
            Path(args.raw).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.sql:
        from judge.banks import Bank  # noqa: F401
        from judge.core import JudgeResult
        rows = []
        for d in results:
            jr = JudgeResult(bank=d["bank"], bank_version=d["bank_version"], bank_sha256=d["bank_sha256"], model=d["model"],
                             subject_type=d["subject_type"], subject_id=d["subject_id"], items=d["items"], meta=d.get("meta") or {})
            rows.extend(ledger_rows(jr, bank, run_tag=args.run_tag))
        Path(args.sql).write_text(rows_to_sql(rows), encoding="utf-8")

    # ── 比对 ──
    qorder = bank.ids(); byid = bank.by_id()
    stat = {q: defaultdict(int) for q in qorder}; p_agree, p_dis = defaultdict(list), defaultdict(list)
    dis, ev_checks = [], defaultdict(lambda: [0, 0])
    for d in results:
        sid = d["subject_id"]
        for qid in qorder:
            it = d["items"].get(qid, {}); ja = it.get("answer")
            if ja is None:
                stat[qid]["jev_null"] += 1
            elif it.get("ambiguous"):
                stat[qid]["jev_amb"] += 1
            oa = (opus.get(sid, {}).get(qid) or {}).get("answer")
            if oa is not None and ja is not None:
                stat[qid]["cmp_opus"] += 1
                if oa == ja:
                    stat[qid]["agree_opus"] += 1; p_agree[qid].append(it.get("p") or 0)
                else:
                    p_dis[qid].append(it.get("p") or 0); dis.append((sid, qid, oa, ja, it.get("p"), it.get("ambiguous")))
                oe = (opus.get(sid, {}).get(qid) or {}).get("evidence"); je = it.get("evidence")
                if oa == ja and oe and byid[qid].jtype == "noul" and ja == "是":
                    ev_checks[qid][1] += 1
                    a_, b_ = norm(oe), norm(je or ""); short, long_ = (a_, b_) if len(a_) <= len(b_) else (b_, a_)
                    if b_ and (short in long_ or overlap(short, long_) >= 0.6):
                        ev_checks[qid][0] += 1
            ga = (gate1.get(sid, {}).get(qid) or {}).get("answer")
            if ga is not None and ja is not None:
                stat[qid]["cmp_g1"] += 1; stat[qid]["agree_g1"] += ga == ja
            if oa is not None and ga is not None:
                stat[qid]["cmp_og"] += 1; stat[qid]["agree_og"] += oa == ga
    n_calls = sum(d.get("calls", 1) for d in results)
    tok_in = sum((d.get("usage") or {}).get("input_tokens", 0) for d in results)
    tok_out = sum((d.get("usage") or {}).get("output_tokens", 0) for d in results)
    lat = [x for d in results for x in (d.get("latency_ms") or [])]
    ev_asked = sum(1 for d in results for it in d["items"].values() if "evidence_p" in it)
    ev_none = sum(1 for d in results for it in d["items"].values() if it.get("invalid_reason") == "evidence_not_found")
    ev_tot = sum(v[1] for v in ev_checks.values()); ev_hit = sum(v[0] for v in ev_checks.values())
    amb_total = sum(s["jev_amb"] for s in stat.values()); cells = sum(1 for d in results for it in d["items"].values() if it.get("answer") is not None)

    def pct(a, b): return f"{a}/{b} ({a / b * 100:.0f}%)" if b else "–"
    used_ext = sorted({c.get("extractor") for q in opus.values() for c in q.values() if c.get("extractor")})
    L = [f"# 特征层影子跑 · {bank.name} {bank.version} × {bank.model} · {len(results)} 篇", "",
         (f"现行抽取器：{'、'.join(used_ext)}（同篇同题多个模型时取最新）。" if used_ext else ""),
         (f"切标题按项目 mapping：{', '.join(f'{k}={v}' for k, v in sorted(modes.items()) if k in {n.get('project_id') for n in notes})}。" if modes else f"切标题：{args.title_extraction}。"), "",
         f"调用 {n_calls} 次，输入 {tok_in:,} / 输出 {tok_out:,} token，单次平均 {sum(lat) / max(len(lat), 1):.0f} ms（最慢 {max(lat) if lat else 0} ms）。"
         f"歧义格 {amb_total}/{cells}（{amb_total / max(cells, 1) * 100:.1f}%）。", "",
         f"证据选句：要证据 {ev_asked} 格，选「没有」{ev_none} 格；与现行抽取器同答「是」且有证据的 {ev_tot} 格里，两边证据重合（子串或 4 字片段 60% 以上）{ev_hit} 格（{ev_hit / ev_tot * 100 if ev_tot else 0:.0f}%）。", "",
         "| 题 | 型 | Jev vs 现行 | Jev vs D-081 表 | 现行 vs D-081 表 | Jev 歧义 | Jev NULL | 一致时 p | 不一致时 p |", "|---|---|---|---|---|---|---|---|---|"]
    tot = defaultdict(int)
    for qid in qorder:
        s = stat[qid]
        for k in ("cmp_opus", "agree_opus", "cmp_g1", "agree_g1", "cmp_og", "agree_og", "jev_amb", "jev_null"):
            tot[k] += s[k]
        pa = f"{sum(p_agree[qid]) / len(p_agree[qid]):.2f}" if p_agree[qid] else "–"
        pd = f"{sum(p_dis[qid]) / len(p_dis[qid]):.2f}" if p_dis[qid] else "–"
        L.append(f"| {qid} | {byid[qid].jtype} | {pct(s['agree_opus'], s['cmp_opus'])} | {pct(s['agree_g1'], s['cmp_g1'])} | "
                 f"{pct(s['agree_og'], s['cmp_og'])} | {s['jev_amb']} | {s['jev_null']} | {pa} | {pd} |")
    L.append(f"| **合计** | | {pct(tot['agree_opus'], tot['cmp_opus'])} | {pct(tot['agree_g1'], tot['cmp_g1'])} | "
             f"{pct(tot['agree_og'], tot['cmp_og'])} | {tot['jev_amb']} | {tot['jev_null']} | | |")
    L += ["", "## 与现行抽取器不一致的格子（篇 · 题 · 现行 → Jev · Jev 概率 · 歧义）", ""]
    L += [f"- {sid} · {qid} · {oa} → {ja} · {p} · {'歧义' if amb else ''}" for sid, qid, oa, ja, p, amb in sorted(dis, key=lambda x: (x[1], x[0]))]
    L += ["", "## 证据核对（按题）", "", "| 题 | 两边证据重合 |", "|---|---|"]
    L += [f"| {qid} | {ev_checks[qid][0]}/{ev_checks[qid][1]} |" for qid in qorder if ev_checks[qid][1]]
    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:5])); print(f"报告 → {args.out}")


if __name__ == "__main__":
    main()
