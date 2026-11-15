"""评论回填：运营写的评论和巡查摘要里的读者评论分开算（TV notes_v1_27 的 source / is_truncated / observed_at）。

全部是合成文本。三列还不存在时行为同以前。
行的形状照 TV 实际写的（truth-vault scripts/sync_comments_from_raw_extra.py、schemas/notes_v1_27 的列注释）：
source 只有 NULL / linkcheck_digest / linkcheck_records；命中预埋关键词 = is_scripted true（没命中是 NULL，不写 false）；
贴主自己的回复 = comment_role 贴主；raw_extra 只有 likes / platform_comment_id，不带任何标记。"""
import csv
import io
import json
import sys
import urllib.error
import urllib.parse

import pytest

from scripts import backfill_comments as B
from tests.test_comment_api import Answers

DIGEST_SOURCE = "linkcheck_digest"          # 「评论区快照」拆出来的
RECORDS_SOURCE = "linkcheck_records"        # 「评论区明细」读出来的


def _c(cid, note="n1", **extra):
    return {"comment_id": cid, "note_id": note, "comment_text": f"合成评论{cid}怎么买", "comment_role": "路人",
            "raw_content": "正文：用于测试的合成帖子，不是真实原文。", **extra}


@pytest.fixture(autouse=True)
def _fresh_column_state(monkeypatch):
    monkeypatch.setattr(B, "_columns", {"present": None, "warned": False})


@pytest.mark.parametrize("row, kind", [
    ({}, B.OPERATOR),                                                    # v1.27 之前的行 / 本地输入
    ({"source": None}, B.OPERATOR),
    ({"source": None, "is_scripted": True, "comment_role": "贴主"}, B.OPERATOR),      # 运营写的行不看这两列
    ({"source": DIGEST_SOURCE, "is_scripted": None, "comment_role": "路人"}, B.DIGEST),
    ({"source": RECORDS_SOURCE, "is_scripted": None, "comment_role": "未知"}, B.DIGEST),
    ({"source": DIGEST_SOURCE, "is_scripted": False}, B.DIGEST),
    ({"source": DIGEST_SOURCE, "is_scripted": True, "raw_extra": {"likes": 3}}, B.SEEDED),
    ({"source": RECORDS_SOURCE, "is_scripted": True, "raw_extra": {"platform_comment_id": "x"}}, B.SEEDED),
    ({"source": DIGEST_SOURCE, "is_scripted": None, "comment_role": "贴主"}, B.AUTHOR),
    ({"source": RECORDS_SOURCE, "comment_role": "贴主"}, B.AUTHOR),
    ({"source": RECORDS_SOURCE, "is_scripted": True, "comment_role": "贴主"}, B.SEEDED),
    ({"source": "somewhere_new"}, B.UNKNOWN),                            # 认不出的不猜
    ({"source": "operator_cell"}, B.UNKNOWN),                            # TV 不写这个值：不猜
])
def test_source_kind(row, kind):
    assert B.source_kind(row) == kind


def _run(monkeypatch, tmp_path, comments, *extra):
    source, rows, summary, raw = (tmp_path / n for n in ("input.json", "rows.json", "summary.csv", "raw.json"))
    source.write_text(json.dumps(comments), encoding="utf-8")
    fake = Answers()
    monkeypatch.setattr(B, "JevClient", lambda **kw: fake)
    monkeypatch.setattr(sys, "argv", ["backfill_comments", "--input", str(source), "--rows-json", str(rows),
                                      "--summary", str(summary), "--raw", str(raw), "--workers", "1", *extra])
    B.main()
    with summary.open(encoding="utf-8", newline="") as fh:
        table = list(csv.reader(fh))
    return fake, json.loads(rows.read_text()), table, json.loads(raw.read_text())


def test_digest_rows_get_reader_bank_only_and_seeded_or_unknown_rows_get_nothing(monkeypatch, tmp_path, capsys):
    comments = [_c("op1"), _c("op2", source=None, is_scripted=None),
                _c("rd1", source=DIGEST_SOURCE, is_truncated=True, observed_at="2026-11-14T02:00:00+00:00", is_scripted=None),
                _c("rd2", source=RECORDS_SOURCE, is_truncated=False, is_scripted=None, raw_extra={"likes": 2}),
                _c("sd1", source=DIGEST_SOURCE, is_scripted=True),
                _c("sd2", source=RECORDS_SOURCE, is_scripted=True, raw_extra={"likes": 1, "platform_comment_id": "x"}),
                _c("au1", source=RECORDS_SOURCE, comment_role="贴主", is_scripted=None),
                _c("un1", source="somewhere_new")]
    fake, rows, table, raw = _run(monkeypatch, tmp_path, comments)
    asked = [set(call["questions"]) for call in fake.calls]
    assert len(fake.calls) == 2 * 2 + 2, "运营写的两套题库各一次；读者评论只有读者侧一次；预埋命中 / 贴主回复 / 认不出的不发请求"
    per = {}
    for r in rows:
        per.setdefault(r["subject_id"], set()).add(r["question_id"])
    assert set(per) == {"op1", "op2", "rd1", "rd2"}
    assert "comment_intent" in per["op1"] and "comment_intent" in per["op2"]
    assert "comment_intent" not in per["rd1"] and "speech_act" in per["rd1"], "读者评论不跑运营侧题库"
    assert sum("comment_intent" in q for q in asked) == 2
    header, line = table[0], dict(zip(table[0], table[1]))
    assert header[:2] == ["note_id", "comments"] and "reader_comments" in header
    assert line["comments"] == "2", "原来那一列只数运营写的，没把读者评论加进去"
    assert (line["reader_comments"], line["reader_truncated"], line["seeded_hits_skipped"],
            line["author_replies_skipped"]) == ("2", "1", "2", "1"), "预埋命中和贴主回复各占一列，都没进读者评论"
    reader_cols = [h for h in header if h.startswith(B.READER_PREFIX)]
    plain_cols = [h for h in header[2:] if not h.startswith((B.READER_PREFIX, "运营:")) and h not in
                  ("reader_comments", "reader_truncated", "seeded_hits_skipped", "author_replies_skipped")]
    assert reader_cols and sum(int(line[h]) for h in reader_cols) == 2 and sum(int(line[h]) for h in plain_cols) == 2
    marked = {e["comment_id"]: e for e in raw if e.get("source_kind")}
    assert set(marked) == {"rd1", "rd2"} and marked["rd1"]["is_truncated"] is True
    assert marked["rd1"]["observed_at"].startswith("2026-11-14")
    out = capsys.readouterr().out
    assert "运营写的 2" in out and "读者评论 2（" in out and "命中预埋关键词不算读者评论 2" in out
    assert "贴主自己的回复不算读者评论 1" in out and "没跑 1" in out


def test_a_note_with_only_author_replies_and_seeded_hits_has_no_reader_comments(monkeypatch, tmp_path):
    comments = [_c("au1", note="n9", source=DIGEST_SOURCE, comment_role="贴主"),
                _c("au2", note="n9", source=DIGEST_SOURCE, comment_role="贴主"),
                _c("sd1", note="n9", source=DIGEST_SOURCE, is_scripted=True)]
    fake, rows, table, raw = _run(monkeypatch, tmp_path, comments)
    assert not fake.calls and rows == [] and raw == []
    assert len(table) == 2, "一篇一行，不因为两种都有就出两行"
    line = dict(zip(table[0], table[1]))
    assert (line["note_id"], line["comments"], line["reader_comments"], line["seeded_hits_skipped"],
            line["author_replies_skipped"]) == ("n9", "0", "0", "1", "2")


def test_without_source_everything_is_as_before(monkeypatch, tmp_path, capsys):
    fake, rows, table, raw = _run(monkeypatch, tmp_path, [_c("c1"), _c("c2")])
    assert len(fake.calls) == 4 and len(rows) == 18
    assert "reader_comments" not in table[0] and not any(h.startswith(B.READER_PREFIX) for h in table[0])
    assert table[1][:2] == ["n1", "2"]
    assert not any("source_kind" in e for e in raw)
    assert "来源：" not in capsys.readouterr().out


def test_source_switch_runs_one_side_only(monkeypatch, tmp_path):
    comments = [_c("op1"), _c("rd1", source=DIGEST_SOURCE), _c("sd1", source=DIGEST_SOURCE, is_scripted=True),
                _c("au1", source=DIGEST_SOURCE, comment_role="贴主")]
    fake, rows, table, _ = _run(monkeypatch, tmp_path, comments, "--source", "digest")
    assert {r["subject_id"] for r in rows} == {"rd1"} and len(fake.calls) == 1
    fake, rows, table, _ = _run(monkeypatch, tmp_path, comments, "--source", "operator")
    assert {r["subject_id"] for r in rows} == {"op1"} and "reader_comments" not in table[0]


def test_resume_rows_of_a_row_that_no_longer_counts_are_dropped(monkeypatch, tmp_path):
    """上一轮判过的行，这一轮来源成了「命中预埋关键词」：旧答案不再出账本，也不进统计。"""
    first = [_c("x1", source=DIGEST_SOURCE)]
    _, rows, _, _ = _run(monkeypatch, tmp_path, first)
    assert rows
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(rows), encoding="utf-8")
    again = [_c("x1", source=DIGEST_SOURCE, is_scripted=True)]
    fake, rows2, table, _ = _run(monkeypatch, tmp_path, again, "--resume", str(prev))
    assert rows2 == [] and not fake.calls
    assert dict(zip(table[0], table[1]))["seeded_hits_skipped"] == "1"


def _http_400(code, message):
    body = json.dumps({"code": code, "message": message}).encode("utf-8")
    return urllib.error.HTTPError("https://example.invalid", 400, "Bad Request", {}, io.BytesIO(body))


def _wire(monkeypatch, comments, *, has_columns, error=None):
    calls = []

    def pg(url, key, path):
        calls.append(path)
        params = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
        offset, limit = int(params["offset"]), int(params["limit"])
        if path.startswith("comments?"):
            wide = "source" in params["select"].split(",")
            assert "raw_extra" not in params["select"], "预埋标记在 is_scripted 列上，不用取 raw_extra"
            assert not wide or "is_scripted" in params["select"].split(",")
            if wide and error is not None:
                raise error
            if wide and not has_columns:
                raise _http_400("42703", "column comments.source does not exist")
            src = [dict(c) for c in comments]
            if "comment_id" in params:
                want = set(urllib.parse.unquote(params["comment_id"])[4:-1].split(","))
                src = sorted((c for c in src if c["comment_id"] in want), key=lambda c: c["comment_id"])
            if not wide:
                src = [{k: v for k, v in c.items() if k not in ("source", "is_truncated", "observed_at", "is_scripted")} for c in src]
        else:
            want = set(urllib.parse.unquote(params["note_id"])[4:-1].split(","))
            src = [{"note_id": n, "title": "合成标题", "raw_content": "正文：合成帖子", "project_id": "P",
                    "projects": {"category": "保健品"}} for n in sorted(want)]
        return src[offset:offset + limit]
    monkeypatch.setattr(B, "_pg", pg)
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    return calls


DB_ROWS = [{"comment_id": "a1", "note_id": "n1", "content": "合成评论一", "comment_role": "素人", "is_pinned": False,
            "source": None, "is_truncated": None, "observed_at": None, "is_scripted": None},
           {"comment_id": "a2", "note_id": "n1", "content": "合成评论二", "comment_role": "路人", "is_pinned": False,
            "source": DIGEST_SOURCE, "is_truncated": True, "observed_at": "2026-11-14T02:00:00+00:00", "is_scripted": None},
           {"comment_id": "a3", "note_id": "n1", "content": "合成评论三", "comment_role": "路人", "is_pinned": False,
            "source": RECORDS_SOURCE, "is_truncated": False, "observed_at": "2026-11-14T02:00:00+00:00", "is_scripted": True},
           {"comment_id": "a4", "note_id": "n1", "content": "合成评论四", "comment_role": "贴主", "is_pinned": False,
            "source": DIGEST_SOURCE, "is_truncated": False, "observed_at": None, "is_scripted": None}]


def test_missing_columns_fall_back_to_the_old_select_and_warn_once(monkeypatch, capsys):
    calls = _wire(monkeypatch, DB_ROWS, has_columns=False)
    got = B.fetch(limit=10, project=None)
    assert [c["comment_id"] for c in got] == ["a1", "a2", "a3", "a4"]
    assert all(B.source_kind(c) == B.OPERATOR for c in got), "三列不存在：所有行按运营写的处理，同以前"
    wide = [p for p in calls if p.startswith("comments?") and "source" in p]
    assert len(wide) == 1, "只试一次"
    B.fetch(limit=10, project=None)
    B.fetch_ids(B.snapshot_of(got))
    assert len([p for p in calls if p.startswith("comments?") and "source" in p]) == 1, "这一轮不再重试"
    assert capsys.readouterr().err.count("notes_v1_27") == 1, "只提示一次"
    assert all("source_kind" not in s for s in B.snapshot_of(got)), "快照形状同以前"


def test_present_columns_are_read_and_split(monkeypatch, capsys):
    _wire(monkeypatch, DB_ROWS, has_columns=True)
    got = B.fetch(limit=10, project=None)
    assert [B.source_kind(c) for c in got] == [B.OPERATOR, B.DIGEST, B.SEEDED, B.AUTHOR]
    assert capsys.readouterr().err == ""
    snap = B.snapshot_of(got)
    assert "source_kind" not in snap[0] and [x["source_kind"] for x in snap[1:]] == [B.DIGEST, B.SEEDED, B.AUTHOR]
    assert "合成评论" not in json.dumps(snap, ensure_ascii=False), "快照仍然不带原文"
    assert [c["comment_id"] for c in B.fetch_ids(snap)] == ["a1", "a2", "a3", "a4"]


@pytest.mark.parametrize("error", [
    _http_400("42703", "column comments.content does not exist"),        # 别的列没了：不是这回事
    _http_400("PGRST100", "failed to parse select parameter"),
    urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"{}")),
])
def test_other_errors_are_not_swallowed(monkeypatch, error):
    _wire(monkeypatch, DB_ROWS, has_columns=True, error=error)
    with pytest.raises(urllib.error.HTTPError):
        B.fetch(limit=10, project=None)


def test_resume_refuses_when_a_row_changed_sides_but_accepts_old_snapshots(monkeypatch, capsys):
    _wire(monkeypatch, DB_ROWS, has_columns=True)
    snap = B.snapshot_of(B.fetch(limit=10, project=None))
    old_shape = [{k: v for k, v in s.items() if k != "source_kind"} for s in snap[:1]]      # v1.27 之前存的快照
    assert [c["comment_id"] for c in B.fetch_ids(old_shape)] == ["a1"]
    moved = [dict(r) for r in DB_ROWS]
    moved[0]["source"] = DIGEST_SOURCE                    # 运营写的 → 巡查来的：要跑的题库整套变了
    _wire(monkeypatch, moved, has_columns=True)
    with pytest.raises(ValueError, match="对不上"):
        B.fetch_ids(snap)
    moved = [dict(r) for r in DB_ROWS]
    moved[1]["source"] = None                             # 巡查来的 → 运营写的
    _wire(monkeypatch, moved, has_columns=True)
    with pytest.raises(ValueError, match="对不上"):
        B.fetch_ids(snap)
    moved = [dict(r) for r in DB_ROWS]
    moved[1]["content"] = "合成评论二改过"
    _wire(monkeypatch, moved, has_columns=True)
    with pytest.raises(ValueError, match="对不上"):
        B.fetch_ids(snap)
    capsys.readouterr()
    moved = [dict(r) for r in DB_ROWS]
    moved[1]["is_scripted"] = True                        # 运营改了评论关键词：同一行从读者评论变成命中预埋关键词，正文没变
    _wire(monkeypatch, moved, has_columns=True)
    got = B.fetch_ids(snap)
    assert [B.source_kind(c) for c in got] == [B.OPERATOR, B.SEEDED, B.SEEDED, B.AUTHOR], "不拒绝；按现在的归类跑"


def test_resume_skips_linkcheck_rows_the_nightly_sync_replaced_but_still_refuses_for_operator_rows(monkeypatch, capsys):
    _wire(monkeypatch, DB_ROWS, has_columns=True)
    snap = B.snapshot_of(B.fetch(limit=10, project=None))
    capsys.readouterr()
    _wire(monkeypatch, [r for r in DB_ROWS if r["comment_id"] not in ("a2", "a4")], has_columns=True)
    got = B.fetch_ids(snap)
    assert [c["comment_id"] for c in got] == ["a1", "a3"], "首页评论区变了、旧摘要行被同步删掉：跳过，其余照常"
    err = capsys.readouterr().err
    assert "2 条巡查来的评论" in err and "合成评论" not in err
    _wire(monkeypatch, [r for r in DB_ROWS if r["comment_id"] != "a1"], has_columns=True)
    with pytest.raises(ValueError, match="1 条快照里的评论生产里已经找不到"):
        B.fetch_ids(snap)
    old_shape = [{k: v for k, v in s.items() if k != "source_kind"} for s in snap]          # 老快照没有这个键 = 都当运营写的
    _wire(monkeypatch, [r for r in DB_ROWS if r["comment_id"] != "a2"], has_columns=True)
    with pytest.raises(ValueError, match="已经找不到"):
        B.fetch_ids(old_shape)


def test_resume_run_drops_the_paid_answers_of_a_vanished_digest_row_and_writes_the_rest(monkeypatch, tmp_path, capsys):
    """整条续跑：上一轮判过三条，其中一条摘要行夜里被同步换掉 —— 续跑不拒绝，剩下两条的旧答案原样进账本，不再发请求。"""
    fake = Answers()
    monkeypatch.setattr(B, "JevClient", lambda **kw: fake)
    ids, rows, rows2 = tmp_path / "ids.json", tmp_path / "rows.json", tmp_path / "rows2.json"
    two = [dict(r, is_scripted=None) for r in DB_ROWS[:3]]          # a1 运营写的, a2 / a3 读者评论
    _wire(monkeypatch, two, has_columns=True)
    monkeypatch.setattr(sys, "argv", ["backfill_comments", "--from-db", "--input-snapshot", str(ids),
                                      "--rows-json", str(rows), "--workers", "1"])
    B.main()
    first = json.loads(rows.read_text())
    assert {r["subject_id"] for r in first} == {"a1", "a2", "a3"}
    calls_before = len(fake.calls)
    _wire(monkeypatch, [r for r in two if r["comment_id"] != "a2"], has_columns=True)
    monkeypatch.setattr(sys, "argv", ["backfill_comments", "--resume-ids", str(ids), "--resume", str(rows),
                                      "--rows-json", str(rows2), "--workers", "1"])
    B.main()
    again = json.loads(rows2.read_text())
    assert {r["subject_id"] for r in again} == {"a1", "a3"} and len(fake.calls) == calls_before
    key = lambda r: (r["subject_id"], r["question_id"])            # noqa: E731
    assert sorted(again, key=key) == sorted((r for r in first if r["subject_id"] != "a2"), key=key)
    assert "1 条巡查来的评论" in capsys.readouterr().err


def test_the_limit_counts_rows_of_the_chosen_source(monkeypatch, tmp_path):
    """先挑来源再截条数：第一条是读者评论、只跑运营写的、limit 1 —— 跑到的是后面那条运营写的，不是空。"""
    comments = [_c("rd1", source=DIGEST_SOURCE), _c("op1"), _c("op2")]
    fake, rows, _, _ = _run(monkeypatch, tmp_path, comments, "--source", "operator", "--limit", "1")
    assert {r["subject_id"] for r in rows} == {"op1"}
    fake, rows, _, _ = _run(monkeypatch, tmp_path, [_c("op1"), _c("rd1", source=DIGEST_SOURCE)],
                            "--source", "digest", "--limit", "1")
    assert {r["subject_id"] for r in rows} == {"rd1"}
