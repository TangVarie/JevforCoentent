# -*- coding: utf-8 -*-
"""外部语料缺写库密钥时不能先花钱、不能绿着收尾（2026-11-04，审计 11-02 F16）。

以前 external-corpus.yml 缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 时：known_ids 退 0（空名单）→ 照样抓取、判定（花钱）→
apply_rows 打一行提示退 0 → job 绿、actions/cache 把 seen state 存下来 → 下周这批按 seen 跳过，只剩 artifact 能手动重放。
现在：workflow 一开始跑 apply_rows.py --check-env，缺就退 1；known_external_ids / apply_rows 在 GITHUB_ACTIONS=true 下缺密钥也退非零。
本机跑（没有 GITHUB_ACTIONS）的老行为不变：known_ids 空、rows 留给人拿 SQL 编辑器跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.apply_rows as A  # noqa: E402
import scripts.known_external_ids as K  # noqa: E402

SECRET = "sk-test-not-a-real-key-000"


def _no_db(*a, **k):
    raise AssertionError("缺密钥时不该发任何请求")


@pytest.fixture
def no_secrets(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(A, "postgrest_upsert", _no_db)
    monkeypatch.setattr(K, "_get", _no_db)


@pytest.mark.parametrize("present", [(), ("SUPABASE_URL",), ("SUPABASE_SERVICE_ROLE_KEY",)])
def test_check_env_fails_when_any_secret_missing(no_secrets, monkeypatch, capsys, present):
    for k in present:
        monkeypatch.setenv(k, SECRET)
    assert A.main(["--check-env"]) == 1
    err = capsys.readouterr().err
    assert "::error::" in err and SECRET not in err
    assert all(k not in err.split("：")[0] for k in present), "只点名缺的那个"


def test_check_env_passes_without_printing_values(no_secrets, monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SECRET)
    assert A.main(["--check-env"]) == 0                          # 只看在不在，不联网（postgrest_upsert 被换成会炸的）
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and "x.supabase.co" not in out.out + out.err


def test_check_env_treats_blank_secret_as_missing(no_secrets, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "  ")
    assert A.main(["--check-env"]) == 1


def test_apply_rows_without_secrets_is_red_in_actions_only(no_secrets, monkeypatch, tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps({"run_id": "ext-t", "external_notes": [{"note_id": "n1"}], "answers": []}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert A.main([str(rows)]) == 1
    assert "::error::" in capsys.readouterr().err
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert A.main([str(rows)]) == 0, "本机跑照旧：不写库、提示拿 SQL 编辑器跑"


def test_known_ids_without_secrets_is_red_in_actions_only(no_secrets, monkeypatch, tmp_path, capsys):
    out = tmp_path / "known.txt"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert K.main([str(out)]) == 1
    assert "::error::" in capsys.readouterr().err
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert K.main([str(out)]) == 0 and out.read_text(encoding="utf-8") == "", "本机跑照旧：空名单、去重只靠 state"


def test_apply_rows_with_secrets_writes_both_tables_in_actions(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(A, "postgrest_upsert", lambda rows, url, key, table="note_feature_answers": seen.append((table, len(rows))) or len(rows))
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SECRET)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps({"run_id": "ext-t", "external_notes": [{"note_id": "n1"}], "answers": [{"subject_id": "n1"}] * 3}), encoding="utf-8")
    assert A.main([str(rows)]) == 0
    assert seen == [("external_notes", 1), ("note_feature_answers", 3)], "外部笔记表在前、账本在后（老顺序不变）"
