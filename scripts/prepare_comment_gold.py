#!/usr/bin/env python3
"""J-03: export two blank, independently ordered human annotation sheets. No model calls."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.banks import build_questions, load_bank
from judge.comments import comment_state, fill_for
from scripts.backfill_comments import post_for

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "fixtures/local/comment_health_annotation_20261012.json"


def spreadsheet_cell(value):
    # Platform text is untrusted. CSV quoting alone does not neutralize spreadsheet formulas.
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n"))
                                   or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def prepare(sample: Path, output: Path):
    data = json.loads(sample.read_text(encoding="utf-8"))
    comments = data["notes"]
    ids = [c["comment_id"] for c in comments]
    if len(ids) != 100 or len(set(ids)) != 100:
        raise ValueError("J-03 requires exactly 100 unique comments")
    bank = load_bank(ROOT / "banks/comment_reader_v0.4.yaml")
    output.mkdir(parents=True, exist_ok=True)
    context, definitions = [], []
    for c in comments:
        post = post_for(c)
        fill = fill_for(post)
        st = comment_state(post, c["comment_text"], role=c.get("comment_role") or "未知")
        context.append({"comment_id": c["comment_id"], "note_id": c["note_id"], "category": c["category"],
                        "角色": c["comment_role"], "帖子标题": st["帖子标题"], "帖子正文": st["帖子正文"],
                        "帖子要点": fill["post_points"], "评论原文": c["comment_text"]})
        definitions.append({"comment_id": c["comment_id"], "state": st,
                            "questions": build_questions(bank, fill=fill)})
    fields = list(context[0]) + [q.id for q in bank.questions] + ["不确定或分歧备注"]
    for person in ("A", "B"):
        rows = list(context)
        random.Random("J03-independent-" + person).shuffle(rows)
        with (output / f"标注{person}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fields)
            writer.writeheader()
            writer.writerows({key: spreadsheet_cell(value) for key, value in row.items()} for row in rows)
    (output / "逐条题面.json").write_text(json.dumps(definitions, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# J-03 大健康评论 · 两人独立标注", "", "同一批 100 条，A/B 顺序不同。分别发给两位标注人；独立完成前不要互看答案。",
             "只看表中的帖子、评论和对应题面，对照下方定义填写七列。是非题填「是 / 否」，选择题填原选项。",
             "拿不准时在备注写原因；不准看模型答案、互动量或运营历史标签。两份都完成后按 comment_id 对齐，分歧由第三人/owner 仲裁。",
             "", "这是数据库文本样本，is_displayed 是历史同步字段，未逐条验证平台当前状态。文本判断不能证明真实亲历，也不用于处方药的对外宣传。",
             "帖子正文统一为切片后的前 600 字；帖子要点从同一段提取。逐条题面.json 给出每条完整填充题面。",
             "", "抽样：OTC药 35、保健品 35、处方药 30；每帖最多 3 条，同项目同文去重，贴主每品类至多 5 条。",
             "处方药均为已入库历史文本；本轮离线标注不发送模型。补齐人工答案、仲裁和逐题模型误差评估后才能开放评论服务。", ""]
    generic = build_questions(bank, fill={**fill_for({"kind": "product", "body": "正文"}),
                                         "post_points": "本条表格的帖子要点", "post_examples": "对应正文里的具体说法"})
    for q in bank.questions:
        item = generic[q.id]
        lines += [f"## {q.id}", "", item["instructions"], ""]
        lines += [f"- {label}：{meaning}" for label, meaning in item["criteria"].items()]
        lines.append("")
    (output / "标注说明.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = {"sample_version": data["sample_version"], "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
                "bank": bank.name, "bank_sha256": bank.sha256, "count": len(ids),
                "categories": dict(Counter(c["category"] for c in comments)),
                "roles": dict(Counter(c["comment_role"] for c in comments)),
                "sampling_query": data["sampling_query"], "answers_prefilled": False, "csv_formula_escaped": True}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=SAMPLE)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    print(json.dumps(prepare(args.sample, args.output), ensure_ascii=False))
