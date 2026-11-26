"""TikHub 非 2xx 返回：报错里带脱敏、截短的 code / message，不带 URL、请求参数回显和别的字段（11-26 外部语料 64 次 400 看不出原因）。
全是合成数据，不联网。"""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import external as E  # noqa: E402
from judge.jev_client import JevClient  # noqa: E402

KEY = "tk-SYNTHETIC-KEY-1234567890"


def _http_error(status: int, body: bytes):
    return urllib.error.HTTPError("https://api.tikhub.io/x?keyword=k", status, "Bad Request", {}, io.BytesIO(body))


def _client(monkeypatch, status: int, body: bytes):
    calls = []

    def fake_urlopen(req, timeout=60):
        calls.append(req.full_url)
        raise _http_error(status, body)

    monkeypatch.setattr(E.urllib.request, "urlopen", fake_urlopen)
    c = E.TikHubClient(api_key=KEY, budget=E.Budget(limit_usd=6.0), rps=1000)
    return c, calls


def test_tikhub_detail_shape_keeps_code_and_message_only(monkeypatch):
    body = json.dumps({"detail": {"code": 400, "message": "Request failed, please retry", "message_zh": "请求失败，请重试",
                                  "router": "/api/v1/xiaohongshu/app_v2/search_notes",
                                  "params": {"keyword": "SECRET_PARAM_ECHO", "token": KEY},
                                  "support": "owner@example.com", "time": "2026-11-26 12:14:07"}}).encode()
    c, _ = _client(monkeypatch, 400, body)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    msg = str(ei.value)
    assert ei.value.status == 400
    assert msg.startswith("HTTP 400：")
    assert "code=400" in msg and "Request failed, please retry" in msg and "请求失败，请重试" in msg
    for leak in ("SECRET_PARAM_ECHO", KEY, "owner@example.com", "router", "search_notes", "api.tikhub.io", "keyword="):
        assert leak not in msg, leak


def test_api_key_and_emails_inside_message_are_masked_and_long_text_is_cut(monkeypatch):
    long_tail = "x" * 1000
    body = json.dumps({"code": 401, "message": f"bad token {KEY} for someone.name+tag@mail.example.org {long_tail}"}).encode()
    c, _ = _client(monkeypatch, 401, body)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    msg = str(ei.value)
    assert KEY not in msg and "mail.example.org" not in msg
    assert "***" in msg and "***@***" in msg
    assert len(msg) < 400 and msg.endswith("…")


def test_fastapi_422_list_shape(monkeypatch):
    body = json.dumps({"detail": [{"loc": ["query", "keyword"], "msg": "field required", "type": "value_error.missing", "input": "ECHO"}]}).encode()
    c, _ = _client(monkeypatch, 422, body)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    msg = str(ei.value)
    assert "loc=query.keyword" in msg and "msg=field required" in msg and "ECHO" not in msg


def test_detail_string_shape(monkeypatch):
    c, _ = _client(monkeypatch, 403, json.dumps({"detail": "Not authenticated"}).encode())
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    assert "detail=Not authenticated" in str(ei.value)


@pytest.mark.parametrize("body, expect", [
    (b"<html><body>Error 1010 owner@example.com</body></html>", "返回不是 JSON（"),
    (b"", None),
    (json.dumps({"data": {"items": []}, "params": {"keyword": "ECHO"}}).encode(), "返回里没有 code / message（"),
])
def test_non_json_empty_and_fieldless_bodies(monkeypatch, body, expect):
    c, _ = _client(monkeypatch, 400, body)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    msg = str(ei.value)
    if expect is None:
        assert msg == "HTTP 400"
    else:
        assert expect in msg
    assert "owner@example.com" not in msg and "ECHO" not in msg and "<html" not in msg


def test_unreadable_body_still_reports_status(monkeypatch):
    class Boom(io.BytesIO):
        def read(self, *a, **k):
            raise OSError("socket closed")

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError("https://api.tikhub.io/x", 502, "Bad Gateway", {}, Boom(b""))

    monkeypatch.setattr(E.urllib.request, "urlopen", fake_urlopen)
    c = E.TikHubClient(api_key=KEY, budget=E.Budget(limit_usd=6.0), rps=1000)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.search("k", 1, "general", None)
    assert str(ei.value) == "HTTP 502" and ei.value.status == 502


def test_detail_request_error_is_still_a_detail_failure_with_the_excerpt(monkeypatch):
    body = json.dumps({"detail": {"code": 400, "message": "upstream failed"}}).encode()
    c, _ = _client(monkeypatch, 400, body)
    with pytest.raises(E.TikHubHTTPError) as ei:
        c.detail("0123456789abcdef01234567")
    assert "upstream failed" in str(ei.value)
    assert not isinstance(ei.value, E.DetailUnparseable)     # 请求层的错不算「返回里找不到正文」，不进放弃计数


def test_run_report_shows_the_excerpt_and_the_run_is_red(monkeypatch):
    """11-26 那样每次搜索都 400：照旧整次红、出错列表每行带 code / message，不带参数回显和密钥。"""
    body = json.dumps({"detail": {"code": 400, "message": "Request failed, please retry", "params": {"keyword": "ECHO"}}}).encode()
    c, calls = _client(monkeypatch, 400, body)
    cfg = yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))
    cfg["categories"] = [{**cfg["categories"][0], "keywords": cfg["categories"][0]["keywords"][:2]}]
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    fq = B.load_bank(ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml", name="feature_questions_v0_1")
    n_sorts = len(cfg["sorts"])
    rep = E.run_once(cfg, c, JevClient(mock=True), triage, fq, {}, known_ids=set())
    assert rep.search_attempts == len(calls) == 2 * n_sorts and rep.searched == 0     # 首页就错：不翻第 2 页
    assert rep.systemic_failure
    report = E.render_report(rep, cfg)
    assert report.count("TikHubHTTPError: HTTP 400：code=400；message=Request failed, please retry") == 2 * n_sorts
    assert "ECHO" not in report and KEY not in report
