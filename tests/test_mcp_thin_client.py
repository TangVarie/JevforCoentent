# -*- coding: utf-8 -*-
"""审计 A-09：写手侧 MCP 是判定服务的薄客户端——Jev 密钥、题库、暗题都不进写手机器。全部离线（urlopen 被替换）。"""
from __future__ import annotations

import ast
import importlib
import io
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BODY = "上周办了张健身卡，第一段讲事。\n第二段讲感受，挺累的但开心。\n大家怎么看，是先戒烟还是边练边戒？"
SERVICE = {"subject_id": "draft", "passed": False, "invalid_reason": None, "profile": {"opening_type": "具体事件"},
           "hard_fails": [["platform_health_v0.1", "efficacy_claim", "是", 0.68, "效果是真的绝"]], "unjudged": [], "ambiguous": [],
           "para_stats": {}, "detail": {}, "para_mode": "none", "paras": [],
           "plan": [{"bank": "platform_health_v0.1", "qid": "efficacy_claim", "instruction": "…"}], "recorded": [{"qid": "opening_type", "why": "闸二"}],
           "calls": 3, "usage": {}, "banks": {"feature_questions_v0_1": {"version": "v0.1", "sha256": "x"}}, "ignored_banks": [],
           "hard_rules": {}, "policy": {"project": "TUGE", "dropped_banks": []}, "rows": 2,
           "ledger_rows": [{"question_id": "para_register", "answer": "文案腔"}], "written": None, "write_error": None}


class _Resp:
    def __init__(self, payload, status=200):
        self._b, self.status = json.dumps(payload, ensure_ascii=False).encode("utf-8"), status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code, detail):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "https://judge.example.test/"); monkeypatch.setenv("JUDGE_API_KEY", "k-writer")
    monkeypatch.setenv("JUDGE_PROJECT", "TUGE"); monkeypatch.setenv("JUDGE_CATEGORY", "教育")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False); monkeypatch.delenv("JUDGE_MOCK", raising=False)
    monkeypatch.delenv("JUDGE_ALLOW_LOCAL_JEV", raising=False)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ── (a) 请求形状：路径、鉴权头、body 与 api.DraftRequest 同形；返回 = 服务端 JSON 减 ledger_rows ──

def test_judge_draft_posts_draft_request_and_returns_service_json_without_ledger_rows(env, monkeypatch):
    from judge import mcp_server as M
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req, timeout)); return _Resp(SERVICE)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen); monkeypatch.setenv("JUDGE_HTTP_TIMEOUT_SEC", "42")
    brief = {"hard_rules": [{"id": "must_ask", "ask": "有没有向读者提问？", "want": True}]}
    d = M.judge_draft("标题", BODY, brief=brief, hard_rules={"platform_health_v0.1:efficacy_claim": "否"}, judge_paras="always")
    (req, timeout), = seen
    assert req.full_url == "https://judge.example.test/judge_draft" and req.get_method() == "POST" and timeout == 42.0
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["x-judge-key"] == "k-writer" and headers["content-type"] == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    from judge.api import DraftRequest
    parsed = DraftRequest.model_validate(body)                                   # 服务端的 Pydantic 模型收得下
    assert parsed.project == "TUGE" and parsed.category == "教育" and parsed.subject_type == "aw_version" and parsed.subject_id == "draft"
    assert parsed.banks == M.DEFAULT_BANKS and parsed.brief == brief and parsed.hard_rules == {"platform_health_v0.1:efficacy_claim": "否"}
    assert parsed.judge_paras == "always" and parsed.write is False and parsed.return_rows is False
    assert body["title"] == "标题" and body["body"] == BODY and "target" not in body and "validated" not in body
    assert "ledger_rows" not in d and d["plan"] == SERVICE["plan"] and d["policy"] == SERVICE["policy"] and d["profile"] == SERVICE["profile"]
    assert "para_register" not in json.dumps(d, ensure_ascii=False)


def test_repair_plan_for_is_one_call_and_returns_plan_and_recorded(env, monkeypatch):
    from judge import mcp_server as M
    sent = []
    monkeypatch.setattr(M, "_post_json", lambda path, body: sent.append((path, body)) or dict(SERVICE))
    out = M.repair_plan_for("标题", BODY, project="NRT", target={"opening_type": "具体事件"}, validated=["opening_type"], subject_id="v1")
    assert len(sent) == 1 and sent[0][0] == "/judge_draft"
    assert sent[0][1]["project"] == "NRT" and sent[0][1]["target"] == {"opening_type": "具体事件"} and sent[0][1]["validated"] == ["opening_type"]
    assert sent[0][1]["subject_id"] == "v1" and sent[0][1]["judge_paras"] == "on_fail" and sent[0][1]["write"] is False
    assert out["plan"] == SERVICE["plan"] and out["recorded"] == SERVICE["recorded"] and out["passed"] is False and "ledger_rows" not in out
    # 服务端响应里没有 plan（版本太旧）：说清楚，不抛
    monkeypatch.setattr(M, "_post_json", lambda path, body: {"results": []})
    bad = M.repair_plan_for("标题", BODY)
    assert "plan" in bad["error"] and bad["keys"] == ["results"]


def test_list_banks_gets_banks_with_key(env, monkeypatch):
    from judge import mcp_server as M
    seen = []
    banks = [{"name": "human_feel_para_v0.2", "version": "v0.2", "questions": ["intent_of_para"], "hidden": 2}]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or _Resp(banks))
    assert M.list_banks() == banks
    (req,) = seen
    assert req.full_url == "https://judge.example.test/banks" and req.get_method() == "GET" and req.data is None
    assert {k.lower(): v for k, v in req.header_items()}["x-judge-key"] == "k-writer"


def test_thin_client_against_real_api(mcp_via_testclient, monkeypatch, policy_cfg):
    """走真 /judge_draft、/banks（TestClient 代替网络，mock Jev）：薄客户端拿到的就是服务端抹过暗题的那一份。"""
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_PROJECT", "TUGE"); monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    from judge import mcp_server as M
    d = M.judge_draft("测试标题？", BODY)
    assert set(d) >= {"passed", "profile", "hard_fails", "para_stats", "plan", "recorded", "policy", "banks"} and d["policy"]["project"] == "TUGE"
    assert "ledger_rows" not in d
    assert M.judge_draft("测试标题？", "太短")["invalid_reason"] == "text_too_short"
    r = M.repair_plan_for("测试标题？", BODY)
    assert isinstance(r["plan"], list) and r["recorded"] == []
    names = {b["name"] for b in M.list_banks()}
    assert {"feature_questions_v0_1", "platform_health_v0.1", "human_feel_para_v0.2"} <= names
    assert [req.full_url for req in mcp_via_testclient] == ["http://judge.test/judge_draft"] * 3 + ["http://judge.test/banks"]


# ── 重试与错误：429 / 5xx 退避重试，401 / 403 / 422 不重试且 detail 原样带回 ──

def test_retries_on_5xx_and_passes_detail_through_on_4xx(env, monkeypatch):
    from judge import mcp_server as M
    codes = iter([503, 429]); seen = []

    def flaky(req, timeout=None):
        seen.append(req)
        try:
            raise _http_error(req.full_url, next(codes), "busy")
        except StopIteration:
            return _Resp(SERVICE)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert M.judge_draft("t", BODY)["plan"] == SERVICE["plan"] and len(seen) == 3                   # 2 次重试后成功
    seen.clear()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or (_ for _ in ()).throw(_http_error(req.full_url, 500, "boom")))
    out = M.judge_draft("t", BODY)
    assert out["status"] == 500 and out["detail"] == "boom" and len(seen) == M.RETRIES + 1          # 重试用尽：error dict
    seen.clear()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or (_ for _ in ()).throw(_http_error(req.full_url, 403, "policy: 处方药项目 OKMAN 的未发布稿不出境")))
    out = M.judge_draft("t", BODY, project="OKMAN")
    assert out["status"] == 403 and out["detail"].startswith("policy:") and len(seen) == 1           # 不重试
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("refused")))
    out = M.list_banks()
    assert "连不上" in out["error"] and out["url"] == "https://judge.example.test/banks"


def test_project_missing_is_refused_client_side_without_a_request(env, monkeypatch):
    from judge import mcp_server as M
    monkeypatch.delenv("JUDGE_PROJECT", raising=False)
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
    for out in (M.judge_draft("t", BODY), M.repair_plan_for("t", BODY)):
        assert out["error"].startswith("policy:") and "JUDGE_PROJECT" in out["error"]
    assert called == []


# ── (b) 评论工具：没有 HTTP 端点，默认拒绝；JUDGE_ALLOW_LOCAL_JEV=1 才走进程内 ──

def test_comment_tools_refuse_without_local_jev_opt_in(env, monkeypatch):
    from judge import mcp_server as M
    calls = []
    monkeypatch.setattr(M, "_client", lambda: calls.append(1)); monkeypatch.setattr(M, "_bank", lambda n: calls.append(n))
    slot = {"id": "q1", "speech_act": ["提问"], "must_echo": False, "value_ok": ["无"], "min_detail": "无"}
    for out in (M.judge_comments("t", BODY, [{"id": "c1", "text": "在哪里"}]),
                M.comment_repair_plan_for("t", BODY, "在哪里", slot),
                M.judge_thread("t", BODY, [{"text": "在哪里"}])):
        assert isinstance(out, dict) and "JUDGE_ALLOW_LOCAL_JEV" in out["error"] and out["local_jev_allowed"] is False
    assert calls == []


def test_comment_tools_run_in_process_only_with_explicit_opt_in(env, monkeypatch):
    """显式放行后仍是原来的进程内路径（mock Jev、本地 banks/），薄客户端的 HTTP 一次都不发。"""
    monkeypatch.setenv("JUDGE_ALLOW_LOCAL_JEV", "1"); monkeypatch.setenv("JUDGE_MOCK", "1")
    from judge import mcp_server as M
    monkeypatch.setattr(M, "_CLIENT", None)
    http = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: http.append(1))
    cs = M.judge_comments("标题", BODY, [{"id": "c1", "text": "在哪里"}])
    assert isinstance(cs, list) and len(cs) == 1 and "speech_act" in cs[0]["items"] and http == []


# ── (c) 模块本身：稿子路径不 import judge.hidden / load_bank / JevClient；模块级只有标准库 + mcp ──

def test_module_source_has_no_in_process_draft_path():
    tree = ast.parse((ROOT / "judge" / "mcp_server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "hidden" not in (node.module or ""), ast.dump(node)                                   # judge.hidden 不进写手机器
            assert not {a.name for a in node.names} & {"all_hidden", "hidden_ids", "setup_draft", "redact", "repair_plan"}, ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any("judge" in a.name for a in node.names), ast.dump(node)
    for node in tree.body:                                                                             # 模块级 import 只有标准库 + mcp
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("judge"), ast.dump(node)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    forbidden = {"JevClient", "load_bank", "discover", "setup_draft", "all_hidden", "hidden_ids", "redact", "repair_plan", "_client", "_bank",
                 "comments", "policy", "BANKS_DIR"}
    for name in ("list_banks", "judge_draft", "repair_plan_for", "_draft_body", "_request", "_post_json", "_get_json", "_config"):
        used = {n.id for n in ast.walk(fns[name]) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fns[name]) if isinstance(n, ast.Attribute)}
        assert not (used & forbidden), (name, used & forbidden)


def test_import_does_not_load_hidden_banks_or_jev_client(monkeypatch):
    for m in [m for m in sys.modules if m == "judge" or m.startswith("judge.")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setenv("JUDGE_URL", "https://judge.example.test"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False); monkeypatch.delenv("TYPESAFE_KEY_FILE", raising=False)
    M = importlib.import_module("judge.mcp_server")
    assert not {"judge.hidden", "judge.banks", "judge.jev_client", "judge.draft", "judge.loop", "judge.comments", "judge.policy"} & set(sys.modules)
    assert {t.name for t in M.mcp._tool_manager.list_tools()} == {"list_banks", "judge_draft", "repair_plan_for", "judge_comments", "comment_repair_plan_for", "judge_thread"}


# ── (d) 没配 JUDGE_URL / JUDGE_API_KEY：启动一条警告，工具返回 error dict，不发请求、不抛 ──

def test_missing_env_warns_at_startup_and_tools_return_error_dict(monkeypatch, caplog):
    monkeypatch.delenv("JUDGE_URL", raising=False); monkeypatch.delenv("JUDGE_API_KEY", raising=False); monkeypatch.setenv("JUDGE_PROJECT", "TUGE")
    from judge import mcp_server as M
    with caplog.at_level(logging.WARNING, logger="judge.mcp_server"):
        M = importlib.reload(M)
    warned = [r for r in caplog.records if r.name == "judge.mcp_server" and r.levelno == logging.WARNING]
    assert len(warned) == 1 and "JUDGE_URL" in warned[0].getMessage() and "JUDGE_API_KEY" in warned[0].getMessage()
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
    for out in (M.judge_draft("t", BODY), M.repair_plan_for("t", BODY), M.list_banks()):
        assert isinstance(out, dict) and out["missing"] == ["JUDGE_URL", "JUDGE_API_KEY"] and "JUDGE_URL" in out["error"]
    assert called == []
    monkeypatch.setenv("JUDGE_URL", "https://judge.example.test")                                       # 只缺一个也说清是哪个
    assert M.list_banks()["missing"] == ["JUDGE_API_KEY"]
    # 三样全缺时先报 HTTP 配置、不是先报 project (codex review on #7): 新机器第一次配能一次看全要填什么
    monkeypatch.delenv("JUDGE_URL", raising=False); monkeypatch.delenv("JUDGE_PROJECT", raising=False)
    for out in (M.judge_draft("t", BODY), M.repair_plan_for("t", BODY)):
        assert out["missing"] == ["JUDGE_URL", "JUDGE_API_KEY"] and "policy" not in out["error"], out
    assert called == []


# ── (e) 200 但不是 JSON / 读到一半断掉：都变成 error dict，后者重试 (codex review on #7) ──

def test_non_json_200_and_truncated_read_become_tool_errors(env, monkeypatch):
    import http.client
    from judge import mcp_server as M
    seen = []

    class _Html:
        status = 200

        def read(self):
            return b"<html><body>502 Bad Gateway</body></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or _Html())
    out = M.judge_draft("t", BODY)
    assert isinstance(out, dict) and "不是 JSON" in out["error"] and out["status"] == 200 and "judge_draft" in out["url"], out
    assert len(seen) == 1, "JUDGE_URL 指错 / 代理回 HTML 不是瞬时的, 不重试"
    assert "error" in M.list_banks() and "error" in M.repair_plan_for("t", BODY)

    seen.clear()
    calls = iter([None, _Resp(SERVICE)])

    def flaky(req, timeout=None):
        seen.append(req)
        nxt = next(calls)
        if nxt is None:
            raise http.client.IncompleteRead(b"{\"pa")
        return nxt
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    out = M.judge_draft("t", BODY)
    assert out.get("passed") is SERVICE["passed"] and len(seen) == 2, "读到一半断掉按瞬时重试一次后成功"
