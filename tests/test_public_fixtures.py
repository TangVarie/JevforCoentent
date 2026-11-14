"""Public demonstrations stay synthetic and exercise real input loaders offline."""
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.backfill_comments import load_input, post_for, state_reader


ROOT = Path(__file__).resolve().parents[1]


def test_public_note_and_comment_inputs_have_no_production_identity():
    notes = json.loads((ROOT / "fixtures/fq_notes_gate1_50.json").read_text())
    assert len(notes) == 50
    assert len({n["project_id"] for n in notes}) == 5
    for note in notes:
        assert note["fixture_kind"] == "synthetic"
        assert note["note_id"].startswith("synthetic_note_")
        assert note["project_id"].startswith("synthetic_project_")
        assert "合成示例" in note["title"] and "完全虚构" in note["raw_content"]
    source = ROOT / "fixtures/comments_ops_sample_33.json"
    data = json.loads(source.read_text())
    assert data["fixture_kind"] == "synthetic" and len(data["notes"]) == 6
    comments = load_input(str(source))
    assert len(comments) == 33 and len({c["comment_id"] for c in comments}) == 33
    for comment in comments:
        assert comment["comment_id"].startswith("synthetic_comment_")
        assert comment["note_id"].startswith("synthetic_comment_note_")
        assert "合成评论" in comment["comment_text"]
        assert post_for(comment)["body"]
        assert state_reader(comment)


def test_synthetic_shadow_is_offline_and_cannot_claim_real_comparison(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/fq_shadow.py"), "--notes",
               str(ROOT / "fixtures/fq_notes_gate1_50.json"), "--mock",
               "--out", str(tmp_path / "report.md")]
    env = dict(os.environ, TYPESAFE_API_KEY="", SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="")
    subprocess.run(command, cwd=ROOT, env=env, check=True, capture_output=True, timeout=30)
    report = (tmp_path / "report.md").read_text()
    assert "合成输入：只验证程序路径" in report
    assert "不是金标准" in report
    private_reference = tmp_path / "reference.tsv"
    private_reference.write_text("synthetic_note_001\ttitle_is_question=是\n")
    failed = subprocess.run(command + ["--gate1", str(private_reference)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert failed.returncode != 0 and "合成输入不能与真实参考答案比对" in failed.stderr
