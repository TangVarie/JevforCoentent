"""J-02 回填：续跑快照不含原文，按 id 回生产重取并逐条比对；读者侧用验收过的 C4（v0.6）。"""
import json
import urllib.parse

import pytest

import scripts.backfill_comments as B

COMMENTS = [{"comment_id": f"c{i}", "note_id": f"n{i // 2}", "content": f"第{i}条：这个怎么买呀", "comment_role": "素人",
             "is_pinned": False} for i in range(5)]
NOTES = [{"note_id": f"n{i}", "title": "标题", "raw_content": f"正文{i}：戒烟第三天，嘴里没味", "project_id": "P",
          "projects": {"category": "保健品"}} for i in range(3)]


def wire(monkeypatch, comments, notes):
    def pg(url, key, path):
        params = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
        offset, limit = int(params["offset"]), int(params["limit"])
        if path.startswith("comments?"):
            if "comment_id" in params:
                want = set(urllib.parse.unquote(params["comment_id"])[4:-1].split(","))
                src = sorted((dict(c) for c in comments if c["comment_id"] in want), key=lambda c: c["comment_id"])
            else:
                src = [dict(c) for c in comments]
        else:
            want = set(urllib.parse.unquote(params["note_id"])[4:-1].split(","))
            src = [dict(n) for n in notes if n["note_id"] in want]
        return src[offset:offset + limit]
    monkeypatch.setattr(B, "_pg", pg)
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def test_snapshot_has_no_raw_text_and_resume_rebuilds_the_same_batch(monkeypatch):
    wire(monkeypatch, COMMENTS, NOTES)
    batch = B.fetch(limit=5, project=None)
    snap = B.snapshot_of(batch)
    blob = json.dumps(snap, ensure_ascii=False)
    assert all(c["content"] not in blob for c in COMMENTS) and all(n["raw_content"] not in blob for n in NOTES)
    shuffled = list(reversed(snap))
    again = B.fetch_ids(shuffled)
    assert [c["comment_id"] for c in again] == [s["comment_id"] for s in shuffled]   # 保持快照顺序
    assert {c["comment_id"]: c["comment_text"] for c in again} == {c["comment_id"]: c["comment_text"] for c in batch}


@pytest.mark.parametrize("change", ["comment_text", "post_body", "missing", "role", "title", "category"])
def test_resume_refuses_when_production_changed(monkeypatch, change):
    wire(monkeypatch, COMMENTS, NOTES)
    snap = B.snapshot_of(B.fetch(limit=5, project=None))
    comments, notes = [dict(c) for c in COMMENTS], [dict(n) for n in NOTES]
    if change == "comment_text":
        comments[1]["content"] += "改"
    elif change == "post_body":
        notes[0]["raw_content"] += "改"
    elif change == "missing":
        comments.pop(3)
    elif change == "role":
        comments[2]["comment_role"] = "贴主"
    elif change == "title":
        notes[1]["title"] = "换了标题"                       # 正文没变，但模型看到的标题变了
    else:
        notes[1]["projects"] = {"category": "教育"}           # 品类决定 agency / product 占位符
    wire(monkeypatch, comments, notes)
    with pytest.raises(ValueError):
        B.fetch_ids(snap)


def test_reader_bank_is_the_accepted_c4():
    bank = B.load_bank(B.READER, name=B.READER_NAME)
    assert B.READER_NAME == "comment_reader_v0.6" and bank.version == "cb-v0.6-candidate4"
    assert bank.sha256 == "a0c04acf37cb05005ed2f3c01ae3b0410221a392da39b665b25879a678cd534d"


def test_workflow_uploads_no_text_snapshot():
    wf = (B.ROOT / ".github/workflows/backfill-comments.yml").read_text(encoding="utf-8")
    assert "--input-snapshot artifacts/ids.json" in wf and "--resume-ids resume/ids.json" in wf
    assert "inputs.json" not in wf and "--input resume" not in wf


def test_cli_takes_exactly_one_source(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["backfill", "--from-db", "--resume-ids", "x.json"])
    with pytest.raises(SystemExit):
        B.main()
    assert "之一" in capsys.readouterr().err
