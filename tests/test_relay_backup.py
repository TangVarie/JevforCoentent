# -*- coding: utf-8 -*-
"""备用中转站（D-148，judge/relay.py + loop.AnthropicCompatGenerator）：共用核心 C1–C12 + 生成端 / /compile_rules / /health。
全部 mock，不联网：主站、备用站都是 .invalid 假地址，urlopen 换成按主机分发的假实现；key 是 sk-test-… 假值。
每个用例结束都查一遍日志：不出现地址、key、中转站报错原文（BALANCE-SENTINEL）。"""
from __future__ import annotations

import glob
import http.client
import io
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import loop as L  # noqa: E402
from judge import relay  # noqa: E402
from judge import rules as R  # noqa: E402

PHOST, BHOST = "primary-host.invalid", "backup-host.invalid"
PURL, BURL = f"https://{PHOST}/v1", f"https://{BHOST}/v1"
PKEY, BKEY = "sk-test-primary-0123456789abcdef", "sk-test-backup-fedcba9876543210"
SENT = "BALANCE-SENTINEL-余额"
SECRETS = (PHOST, BHOST, PKEY, BKEY, SENT, "0123456789abcdef", "fedcba9876543210")
RELAY_ENV = ("ANTHROPIC_BASE_URL_BACKUP", "ANTHROPIC_API_KEY_BACKUP", "RELAY_FORCE", "RELAY_PRIMARY_TIMEOUT_S", "RELAY_SLOW_S",
             "RELAY_BREAKER_FAILS", "RELAY_BREAKER_COOLDOWN_S", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_AUTH_TOKEN")
OTHER_ENV = ("JUDGE_MOCK", "MOONSHOT_API_KEY", "JUDGE_COMPILE_MODEL", "JUDGE_COMPILE_EFFORT", "JUDGE_COMPILE_BATCH",
             "JUDGE_COMPILE_WORKERS", "JUDGE_COMPILE_TIMEOUT_SEC", "JUDGE_COMPILE_RETRIES")
OPEN_LINE = "relay: breaker open for"
FIRST_FAILOVER = "failing over to backup"


# ── 假中转站：按主机分发，每个站一个脚本（最后一项一直重复），记下每次请求的 URL / 超时 / 头 ──

def ok(text="OK", stop="end_turn"):
    return ("ok", text, stop)


def http_err(code, body=None):
    return ("http", code, body if body is not None else json.dumps(
        {"error": {"type": "api_error", "message": f"{SENT} {PKEY} {BKEY}"}}, ensure_ascii=False).encode("utf-8"))


TIMEOUT = ("raise", lambda: TimeoutError(f"read timed out talking to {PHOST} / {BHOST}"))
REFUSED = ("raise", lambda: urllib.error.URLError(ConnectionRefusedError(111, f"Connection refused by {PHOST} / {BHOST}")))
INCOMPLETE = ("raise", lambda: http.client.IncompleteRead(b"partial"))
HTML200 = ("raw", f"<html>{SENT} {PHOST}</html>".encode("utf-8"))


class _Resp:
    def __init__(self, raw: bytes):
        self._raw, self.status = raw, 200

    def read(self, *a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRelays:
    def __init__(self):
        self.script = {"primary": [ok("PRIMARY-TEXT")], "backup": [ok("BACKUP-TEXT")]}
        self.calls: list = []
        self._lock = threading.Lock()

    def set(self, primary=None, backup=None):
        if primary is not None:
            self.script["primary"] = list(primary)
        if backup is not None:
            self.script["backup"] = list(backup)

    def targets(self):
        return [c["target"] for c in self.calls]

    def __call__(self, req, timeout=None):
        url = req.full_url
        target = "backup" if BHOST in url else "primary" if PHOST in url else "other"
        assert target != "other", "测试里不许打到别的地址"
        with self._lock:
            self.calls.append({"target": target, "url": url, "timeout": timeout, "headers": dict(req.header_items()),
                               "data": req.data})
            q = self.script[target]
            step = q.pop(0) if len(q) > 1 else q[0]
        kind = step[0]
        if callable(kind):
            step = kind(req)
            kind = step[0]
        if kind == "ok":
            body = {"content": [{"type": "text", "text": step[1]}], "stop_reason": step[2],
                    "usage": {"input_tokens": 3, "output_tokens": 2}}
            return _Resp(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if kind == "raw":
            return _Resp(step[1])
        if kind == "http":
            raise urllib.error.HTTPError(url, step[1], "Relay Says No", {}, io.BytesIO(step[2]))
        if kind == "raise":
            raise step[1]()
        raise AssertionError(kind)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch, tmp_path, caplog):
    for k in RELAY_ENV + OTHER_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PKEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", PURL)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    relay._reset_for_tests()
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    caplog.set_level(logging.WARNING)
    yield sleeps
    # C10 / 12：每个用例的日志里都不许出现地址、key、中转站报错原文（teardown 时 caplog.records 只剩本阶段的，要按阶段取）
    for rec in caplog.get_records("setup") + caplog.get_records("call"):
        msg = rec.getMessage()
        assert not any(s in msg for s in SECRETS), msg
        assert "://" not in msg, msg
    relay._reset_for_tests()   # 计数清零：进程退出时的 relay summary 不在 pytest 输出里多打一行


@pytest.fixture
def relays(monkeypatch):
    fake = FakeRelays()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def with_backup(monkeypatch, force=None):
    monkeypatch.setenv("ANTHROPIC_BASE_URL_BACKUP", BURL)
    monkeypatch.setenv("ANTHROPIC_API_KEY_BACKUP", BKEY)
    if force is not None:
        monkeypatch.setenv("RELAY_FORCE", force)


def relay_lines(caplog, needle=""):
    return [r.getMessage() for r in caplog.records if r.name == "relay" and needle in r.getMessage()]


def breaker_files(tmp_path):
    return sorted(glob.glob(str(tmp_path / "relay_breaker_*")))


def fresh_breaker(tmp_path):
    for f in breaker_files(tmp_path):
        os.unlink(f)
    relay._reset_for_tests()


def load_breaker(url=PURL):
    with open(relay.state_path(url), encoding="utf-8") as f:
        return json.load(f)


class FakeStatusError(Exception):
    """SDK 风格：status_code + body（dict）。"""

    def __init__(self, status, body=None, msg="relay error"):
        super().__init__(msg)
        self.status_code, self.body = status, body


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


# ══ 共用核心 C1–C12 ═══════════════════════════════════════════════════════

def test_c1_mode_table(monkeypatch, caplog):
    for url, key in (("", ""), (BURL, ""), ("", BKEY), ("   ", BKEY), (BURL, "  \t")):
        monkeypatch.setenv("ANTHROPIC_BASE_URL_BACKUP", url)
        monkeypatch.setenv("ANTHROPIC_API_KEY_BACKUP", key)
        assert relay.mode() == relay.MODE_PRIMARY_ONLY and not relay.backup_configured(), (url, key)
    with_backup(monkeypatch)
    assert relay.backup_configured() and relay.mode() == relay.MODE_AUTO
    for force, want in (("AUTO ", relay.MODE_AUTO), ("auto", relay.MODE_AUTO), ("", relay.MODE_AUTO),
                        ("primary", relay.MODE_PRIMARY_ONLY), (" Backup", relay.MODE_BACKUP_ONLY)):
        monkeypatch.setenv("RELAY_FORCE", force)
        assert relay.mode() == want, force
    assert relay_lines(caplog) == []
    for k in ("ANTHROPIC_BASE_URL_BACKUP", "ANTHROPIC_API_KEY_BACKUP"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("RELAY_FORCE", "backup")
    assert relay.mode() == relay.MODE_PRIMARY_ONLY and relay.mode() == relay.MODE_PRIMARY_ONLY
    assert relay_lines(caplog) == ["relay: RELAY_FORCE=backup but no backup relay is configured; using primary"]
    with_backup(monkeypatch, force="zzz")
    assert relay.mode() == relay.MODE_AUTO and relay.force_setting() == "auto"
    assert relay_lines(caplog, "not recognized") == [
        "relay: RELAY_FORCE not recognized (expected primary, backup or empty); using auto"]


def test_c2_parsing(monkeypatch):
    for raw, want in (("", 90.0), ("20", 20.0), ("1", 5.0), ("9999", 600.0), ("abc", 90.0), ("nan", 90.0), (" 33.5 ", 33.5)):
        monkeypatch.setenv("RELAY_PRIMARY_TIMEOUT_S", raw)
        assert relay.primary_timeout_s(90.0) == want, raw
    for raw, want in (("", 0.0), ("0", 0.0), ("-3", 0.0), ("25", 25.0), ("9999", 600.0), ("x", 0.0)):
        monkeypatch.setenv("RELAY_SLOW_S", raw)
        assert relay.slow_s(0.0) == want, raw
    for raw, want in (("", 2), ("2.5", 2), ("0", 1), ("50", 20), ("3", 3)):
        monkeypatch.setenv("RELAY_BREAKER_FAILS", raw)
        assert relay.breaker_fails() == want, raw
    for raw, want in (("", 1200.0), ("10", 30.0), ("999999", 86400.0), ("60", 60.0)):
        monkeypatch.setenv("RELAY_BREAKER_COOLDOWN_S", raw)
        assert relay.breaker_cooldown_s() == want, raw


def test_c3_classification():
    yes = [FakeStatusError(s) for s in (401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)]
    yes += [L.GeneratorHTTPError(s, "x", "") for s in (401, 402, 404, 503, 529)]
    yes += [urllib.error.HTTPError("https://x.invalid", 503, "x", {}, None)]
    yes += [APITimeoutError(), APIConnectionError(), TimeoutError(), ConnectionResetError(),
            urllib.error.URLError(ConnectionRefusedError()), http.client.RemoteDisconnected(), http.client.IncompleteRead(b""),
            ssl.SSLError(), json.JSONDecodeError("Expecting value", "<html>", 0),
            FakeStatusError(200, {"error": {"type": "overloaded_error"}}), FakeStatusError(200, {"type": "rate_limit_error"})]
    for exc in yes:
        assert relay.is_failover_worthy(exc), repr(exc)
    no = [FakeStatusError(s) for s in (400, 405, 413, 415, 422)]
    no += [L.GeneratorHTTPError(400, "Bad Request", ""), urllib.error.HTTPError("https://x.invalid", 400, "x", {}, None)]
    no += [ValueError(), KeyError("k"), RuntimeError("connection timeout"), RuntimeError("HTTP 503 overloaded")]
    no += [FakeStatusError(500, msg="sensitive_words_detected"), FakeStatusError(529, msg="sensitive_words_detected"),
           APITimeoutError("sensitive_words_detected"),
           L.GeneratorHTTPError(500, "x", '{"error":{"type":"sensitive_words_detected"}}', relay="primary")]
    for exc in no:
        assert not relay.is_failover_worthy(exc), repr(exc)


def test_c4_describe():
    assert relay.describe(FakeStatusError(529, {"error": {"type": "overloaded_error"}}, msg=SENT)) == "HTTP 529 overloaded_error"
    assert relay.describe(APITimeoutError(SENT)) == "APITimeoutError"
    assert relay.describe(FakeStatusError(401, {"error": {"type": "authentication_error"}}, msg=SENT)) == "HTTP 401"
    assert relay.describe(L.GeneratorHTTPError(503, "x", SENT, relay="primary")) == "HTTP 503"
    assert relay.describe(urllib.error.URLError(f"{PHOST}")) == "URLError"


def test_c5_state_path(tmp_path):
    a = relay.state_path(f"https://{PHOST}/")
    assert a == relay.state_path(f"https://{PHOST}/v1") == relay.state_path(f" https://{PHOST}/v1/ ")
    assert a != relay.state_path(f"https://{BHOST}/v1")
    assert os.path.dirname(a) == str(tmp_path)
    name = os.path.basename(a)
    assert PHOST not in name and "http" not in name and re.fullmatch(r"relay_breaker_[0-9a-f]{16}\.json", name)
    assert relay.state_path("") == relay.state_path(None) == relay.state_path(relay.OFFICIAL_BASE_URL)


def test_c6_breaker_phases(monkeypatch, caplog):
    clock = [10_000.0]
    monkeypatch.setattr(relay, "_now", lambda: clock[0])
    p = relay.state_path(PURL)
    relay._record(p, "fail", False, "HTTP 503")
    assert relay._decide(p, 1200.0) == "primary" and load_breaker()["fails"] == 1
    relay._record(p, "fail", False, "HTTP 503")
    assert relay._decide(p, 1200.0) == "skip" and relay._decide(p, 1200.0) == "skip"
    assert relay_lines(caplog, OPEN_LINE) == [
        "relay: breaker open for 1200s after 2 consecutive primary failures (last: HTTP 503); calls go straight to backup"]
    clock[0] += 1199
    assert relay._decide(p, 1200.0) == "skip"
    clock[0] += 1                                                   # opened_at + cooldown → half_open
    assert relay._decide(p, 1200.0) == "probe" and relay._decide(p, 1200.0) == "skip"
    clock[0] += 601                                                 # 探针认领过了 600 秒 → 再放一个
    assert relay._decide(p, 1200.0) == "probe"
    relay._record(p, "ok", True, "")
    assert relay_lines(caplog, "closed") == ["relay: breaker closed (primary ok again)"]
    assert load_breaker() == {"v": 1, "fails": 0, "opened_at": None, "probe_at": None}
    assert relay._decide(p, 1200.0) == "primary"
    # 探针失败 → 重新熔断，opened_at 是新的
    relay._record(p, "fail", False, "x"); relay._record(p, "fail", False, "x")
    first_open = load_breaker()["opened_at"]
    clock[0] += 1200
    assert relay._decide(p, 1200.0) == "probe"
    relay._record(p, "fail", True, "TimeoutError")
    st = load_breaker()
    assert st["opened_at"] == clock[0] > first_open and st["probe_at"] is None and relay._decide(p, 1200.0) == "skip"
    # 探针遇到中性错误 → 只放掉认领
    clock[0] += 1200
    assert relay._decide(p, 1200.0) == "probe"
    relay._record(p, "neutral", True, "")
    assert load_breaker()["probe_at"] is None and relay._decide(p, 1200.0) == "probe"
    # 未来的 opened_at（时钟漂移 / 文件被改坏）读成 half_open
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "fails": 5, "opened_at": clock[0] + 5000, "probe_at": None}, f)
    with_backup(monkeypatch)
    assert relay.breaker_state(PURL) == "half_open" and relay._decide(p, 1200.0) == "probe"


@pytest.mark.parametrize("raw", ["{x", "[]", '{"v":2}', '{"v":1,"fails":"x","opened_at":"y"}', ""])
def test_c7_corrupt_state_file_reads_closed(raw):
    p = relay.state_path(PURL)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    relay._reset_for_tests()
    assert relay._load(p) == relay._fresh()
    assert relay._decide(p, 1200.0) == "primary"
    relay._record(p, "fail", False, "x")
    assert load_breaker() == {"v": 1, "fails": 1, "opened_at": None, "probe_at": None}


def test_c8_cross_process_breaker_counts(tmp_path):
    code = ("import sys, tempfile; sys.path.insert(0, sys.argv[1]); tempfile.tempdir = sys.argv[2]\n"
            "from judge import relay\n"
            "p = relay.state_path(sys.argv[3])\n"
            "for _ in range(50):\n"
            "    relay._record(p, 'fail', False, 'TimeoutError')\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(tmp_path), PURL],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    errs = []
    for pr in procs:
        out, err = pr.communicate(timeout=60)
        assert pr.returncode == 0, err.decode("utf-8", "replace")
        errs.append(err.decode("utf-8", "replace"))
    st = load_breaker()
    assert st["fails"] == 200 and st["opened_at"] is not None
    assert sum(e.count(OPEN_LINE) for e in errs) == 1                # 只有一个进程把它打开
    assert not any(s in "".join(errs) for s in SECRETS)
    assert not glob.glob(str(tmp_path / "*.tmp"))


def test_c9_call_paths(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(relay, "_mono", lambda: clock[0])
    seen: list = []

    def unit(target, took=1.0, fail=None):
        seen.append(target)
        clock[0] += took
        if fail and target == "primary":
            raise fail
        return target

    A = relay.MODE_AUTO
    assert relay.call(unit, relay_mode=A, primary_url=PURL, slow_after_s=25.0) == "primary"
    assert relay.counts()["primary_ok"] == 1 and relay.counts()["primary_slow"] == 0
    # 慢成功：结果照用，但算一次熔断失败；连续两次 → 熔断，第三次直接走备用
    slow = lambda t: unit(t, took=30.0)                                          # noqa: E731
    assert relay.call(slow, relay_mode=A, primary_url=PURL, slow_after_s=25.0) == "primary"
    assert relay.call(slow, relay_mode=A, primary_url=PURL, slow_after_s=lambda: 25.0) == "primary"
    assert relay.counts()["primary_slow"] == 2 and relay_lines(caplog, OPEN_LINE)
    assert "(last: slow 30.0s)" in relay_lines(caplog, OPEN_LINE)[0]
    seen.clear()
    assert relay.call(slow, relay_mode=A, primary_url=PURL, slow_after_s=25.0) == "backup" and seen == ["backup"]
    assert relay.counts()["breaker_open_skips"] == 1
    # 新 URL（新熔断文件）：可切换的错误 → 备用站的结果
    other = f"https://{PHOST}/other"
    seen.clear()
    assert relay.call(lambda t: unit(t, fail=TimeoutError(SENT)), relay_mode=A, primary_url=other) == "backup"
    assert seen == ["primary", "backup"] and relay.counts()["primary_fail"] == 1
    assert relay_lines(caplog, FIRST_FAILOVER) == ["relay: primary failed (TimeoutError); failing over to backup"]
    # 备用站的异常不带主站的 __context__
    boom = RuntimeError("backup boom")

    def both_fail(t):
        if t == "primary":
            raise ConnectionResetError(SENT)
        raise boom

    with pytest.raises(RuntimeError) as ei:
        relay.call(both_fail, relay_mode=A, primary_url=f"https://{PHOST}/third")
    assert ei.value is boom and ei.value.__context__ is None and relay.counts()["backup_fail"] == 1
    # 不该切换的错误：原样抛同一个对象，不碰备用站
    bad = ValueError("bad request")
    seen.clear()
    with pytest.raises(ValueError) as ei:
        relay.call(lambda t: unit(t, fail=bad), relay_mode=A, primary_url=f"https://{PHOST}/fourth")
    assert ei.value is bad and seen == ["primary"]
    # backup_only 从不碰主站；primary_only 不该进 call()
    seen.clear()
    assert relay.call(unit, relay_mode=relay.MODE_BACKUP_ONLY, primary_url=PURL) == "backup" and seen == ["backup"]
    with pytest.raises(ValueError):
        relay.call(unit, relay_mode=relay.MODE_PRIMARY_ONLY, primary_url=PURL)
    assert len(relay_lines(caplog, FIRST_FAILOVER)) == 1                                  # 第一次切换只打一行


def test_c10_c11_summary_and_exit_line(monkeypatch, caplog):
    with_backup(monkeypatch)
    relay._emit_summary()
    assert relay_lines(caplog, "summary") == []                                           # 计数全 0：不打
    relay.call(lambda t: (_ for _ in ()).throw(TimeoutError(f"{PHOST} {SENT}")) if t == "primary" else "ok",
               relay_mode=relay.MODE_AUTO, primary_url=PURL)
    s = relay.summary(PURL)
    assert set(s) == {"mode", *relay.COUNTER_KEYS, "breaker"} and "ok" not in s and "failed" not in s
    assert re.search(r'"(ok|failed)": *\d', json.dumps(s)) is None
    assert s["mode"] == "auto" and s["breaker"] == "closed" and s["primary_fail"] == 1 and s["backup_ok"] == 1
    relay._emit_summary()
    line = relay_lines(caplog, "relay summary")
    assert len(line) == 1 and json.loads(line[0].split(": ", 1)[1]) == relay.summary()


def test_c12_health_shape_and_no_state_file_when_off(monkeypatch, tmp_path):
    h = relay.health(include_counts=True)
    assert h == {"relay_mode": "primary_only", "relay_backup_configured": False, "relay_force": "auto",
                 "relay_breaker": "off", "relay_counts": dict.fromkeys(relay.COUNTER_KEYS, 0)}
    assert breaker_files(tmp_path) == []
    with_backup(monkeypatch)
    h = relay.health(include_counts=False)
    assert h == {"relay_mode": "auto", "relay_backup_configured": True, "relay_force": "auto", "relay_breaker": "closed"}
    assert not any(s in json.dumps(h) for s in SECRETS)
    monkeypatch.setenv("RELAY_FORCE", "backup")
    assert relay.health()["relay_mode"] == "backup_only" and relay.health()["relay_breaker"] == "off"


# ══ 生成端：没配备用站 = 原样 ═══════════════════════════════════════════════

LEGACY_CASES = {
    "none": {},
    "url_only": {"ANTHROPIC_BASE_URL_BACKUP": BURL},
    "key_only": {"ANTHROPIC_API_KEY_BACKUP": BKEY},
    "blank_url": {"ANTHROPIC_BASE_URL_BACKUP": "   ", "ANTHROPIC_API_KEY_BACKUP": BKEY},
    "forced_primary": {"ANTHROPIC_BASE_URL_BACKUP": BURL, "ANTHROPIC_API_KEY_BACKUP": BKEY, "RELAY_FORCE": "primary"},
}


@pytest.mark.parametrize("case", list(LEGACY_CASES))
def test_no_backup_is_legacy_byte_for_byte(case, monkeypatch, relays, sleeps, tmp_path, caplog):
    for k, v in LEGACY_CASES[case].items():
        monkeypatch.setenv(k, v)
    used = []
    orig = L.AnthropicCompatGenerator._post_legacy
    monkeypatch.setattr(L.AnthropicCompatGenerator, "_post_legacy", lambda self, b: used.append(1) or orig(self, b))
    monkeypatch.setattr(relay, "call", lambda *a, **k: pytest.fail("primary_only 不许进 relay.call"))
    g = R.make_generator()
    assert g._relay_mode == relay.MODE_PRIMARY_ONLY and g.retries == 1 and g.timeout == 90.0
    assert g.base_url == f"https://{PHOST}"
    # 连不上：2 次尝试、都打主站、超时 90 秒、退避 0.5 秒，最后原样抛 URLError（不换成 primary: …）
    the_exc = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    relays.set(primary=[("raise", lambda: the_exc)])
    with pytest.raises(urllib.error.URLError) as ei:
        g("p")
    assert ei.value is the_exc and sleeps == [0.5]
    assert relays.targets() == ["primary", "primary"] and {c["timeout"] for c in relays.calls} == {90.0}
    c0 = relays.calls[0]
    assert c0["url"] == f"https://{PHOST}/v1/messages"
    assert c0["headers"] == {"X-api-key": PKEY, "Anthropic-version": "2023-06-01", "Content-type": "application/json"}
    body = {"model": "claude-sonnet-5-5", "max_tokens": 16000, "messages": [{"role": "user", "content": "p"}],
            "output_config": {"effort": "low"}}
    assert c0["data"] == json.dumps(body, ensure_ascii=False).encode("utf-8")
    # 503 → 200：照旧重试一次成功
    relays.calls.clear()
    relays.set(primary=[http_err(503), ok("FINE")])
    assert g("p") == ["FINE"] and relays.targets() == ["primary", "primary"]
    # 400：消息和以前一字不差（不带 [primary] 标签），不重试
    relays.calls.clear()
    relays.set(primary=[http_err(400, b'{"error":{"message":"bad field"}}')])
    with pytest.raises(L.GeneratorHTTPError) as ei:
        g("p")
    assert str(ei.value) == 'HTTP 400 Relay Says No：{"error":{"message":"bad field"}}' and ei.value.relay is None
    assert relays.targets() == ["primary"] and used
    assert breaker_files(tmp_path) == [] and relay.counts() == dict.fromkeys(relay.COUNTER_KEYS, 0)
    assert relay_lines(caplog, "breaker") == [] and relay_lines(caplog, FIRST_FAILOVER) == []


def test_explicit_endpoint_generator_is_never_redirected(monkeypatch, relays):
    with_backup(monkeypatch)
    g = L.AnthropicCompatGenerator("kimi", base_url=f"https://{PHOST}/v1/", api_key=PKEY)          # 显式端点（Kimi 那种）
    assert g._relay_mode == relay.MODE_PRIMARY_ONLY and g.backup_base_url == "" and g.backup_api_key == ""
    g2 = L.AnthropicCompatGenerator("m", base_url=f"https://{PHOST}", api_key=PKEY, backup_base_url=f" {BURL}/ ",
                                    backup_api_key=f" {BKEY} ")
    assert (g2.backup_base_url, g2.backup_api_key, g2._relay_mode) == (f"https://{BHOST}", BKEY, relay.MODE_AUTO)
    monkeypatch.delenv("ANTHROPIC_BASE_URL_BACKUP")
    g3 = L.AnthropicCompatGenerator("m")                                                          # env 只配了一半
    assert g3._relay_mode == relay.MODE_PRIMARY_ONLY and g3.backup_base_url == ""


# ══ 生成端：切换 ═══════════════════════════════════════════════════════════

def test_primary_timeout_fails_over_with_exact_requests(monkeypatch, relays, sleeps, caplog):
    with_backup(monkeypatch)
    relays.set(primary=[TIMEOUT], backup=[ok("BACKUP-TEXT")])
    g = R.make_generator()
    assert g._relay_mode == relay.MODE_AUTO and g.backup_base_url == f"https://{BHOST}"
    assert g("p") == ["BACKUP-TEXT"] and g.calls == 1 and g.usage == {"input_tokens": 3, "output_tokens": 2}
    assert relays.targets() == ["primary", "backup"] and sleeps == []
    p, b = relays.calls
    assert p["url"] == f"https://{PHOST}/v1/messages" and p["headers"]["X-api-key"] == PKEY and p["timeout"] == 90.0
    assert b["url"] == f"https://{BHOST}/v1/messages" and b["headers"]["X-api-key"] == BKEY and b["timeout"] == 90.0
    assert PKEY not in json.dumps(b["headers"]) and BKEY not in json.dumps(p["headers"]) and p["data"] == b["data"]
    assert relay_lines(caplog, FIRST_FAILOVER) == ["relay: primary failed (TimeoutError); failing over to backup"]
    assert relay.counts() == {"primary_ok": 0, "primary_slow": 0, "primary_fail": 1, "backup_ok": 1, "backup_fail": 0,
                              "breaker_open_skips": 0}
    # 主站限时 = JUDGE_COMPILE_TIMEOUT_SEC；RELAY_PRIMARY_TIMEOUT_S 只改主站
    for extra, want_p, want_b in (({}, 40.0, 40.0), ({"RELAY_PRIMARY_TIMEOUT_S": "20"}, 20.0, 40.0)):
        fresh_breaker(Path(tempfile.tempdir))
        monkeypatch.setenv("JUDGE_COMPILE_TIMEOUT_SEC", "40")
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        relays.calls.clear()
        assert R.make_generator()("p") == ["BACKUP-TEXT"]
        assert [(c["target"], c["timeout"]) for c in relays.calls] == [("primary", want_p), ("backup", want_b)]


FAILOVER_OUTCOMES = {**{f"http{s}": http_err(s) for s in (401, 402, 403, 404, 408, 409, 429, 500, 503, 520, 524, 529)},
                     "incomplete_read": INCOMPLETE, "html_200": HTML200, "refused": REFUSED, "timeout": TIMEOUT}


@pytest.mark.parametrize("outcome", list(FAILOVER_OUTCOMES.values()), ids=list(FAILOVER_OUTCOMES))
def test_failover_worthy_primary_outcomes(outcome, monkeypatch, relays):
    with_backup(monkeypatch)
    relays.set(primary=[outcome], backup=[ok("FROM-BACKUP")])
    assert R.make_generator()("p") == ["FROM-BACKUP"]
    assert relays.targets() == ["primary", "backup"]
    assert relay.counts()["primary_fail"] == 1 and relay.counts()["backup_ok"] == 1


@pytest.mark.parametrize("status", [400, 422])
def test_bad_request_on_primary_never_reaches_backup(status, monkeypatch, relays):
    with_backup(monkeypatch)
    relays.set(primary=[http_err(status, b'{"error":{"type":"invalid_request_error","message":"nope"}}')])
    with pytest.raises(L.GeneratorHTTPError) as ei:
        R.make_generator()("p")
    e = ei.value
    assert e.code == status and e.relay == "primary" and str(e).startswith(f"HTTP {status} Relay Says No [primary]：")
    assert relays.targets() == ["primary"] and relay.counts()["backup_ok"] == relay.counts()["backup_fail"] == 0
    assert relay._load(relay.state_path(PURL))["fails"] == 0


def test_gateway_sensitive_words_500_is_not_a_failover(monkeypatch, relays, sleeps):
    with_backup(monkeypatch)
    body = json.dumps({"error": {"type": "sensitive_words_detected", "message": "blocked"}}).encode("utf-8")
    relays.set(primary=[http_err(500, body)])
    with pytest.raises(L.GeneratorHTTPError) as ei:
        R.make_generator()("p")
    assert ei.value.code == 500 and ei.value.relay == "primary" and "sensitive_words_detected" in str(ei.value)
    assert relays.targets() == ["primary", "primary"] and sleeps == [0.5]                    # 500 照旧重试一次，不切
    assert relay._load(relay.state_path(PURL))["fails"] == 0 and relay.counts()["primary_fail"] == 0


def test_backup_http_error_is_labelled_and_both_keys_redacted(monkeypatch, relays):
    with_backup(monkeypatch)
    relays.set(primary=[TIMEOUT], backup=[http_err(402)])
    with pytest.raises(L.GeneratorHTTPError) as ei:
        R.make_generator()("p")
    e, msg = ei.value, str(ei.value)
    assert msg.startswith("HTTP 402 Relay Says No [backup]：") and e.relay == "backup" and e.code == 402
    assert PKEY not in msg and BKEY not in msg and msg.count("***") >= 2
    assert "sk-test" not in msg                                     # 两把 key 都整把换掉（不是只靠 sk-xxxx 的兜底打码）
    assert not isinstance(e.__context__, TimeoutError) and relays.targets() == ["primary", "backup"]   # 402 不重试
    assert relay.counts()["backup_fail"] == 1


# 写作台 deskcore/core.py 的 _FREE_GENERATOR_FAILURE（原样抄过来，deskcore 不改）：没计费的 429 / 5xx 只扣 30 分钟、别的扣 6 小时。
# [primary] / [backup] 标签不能挡住它（放在 HTTP 前面就会让备用站回的 529 被当成计过费的失败、整个池子 6 小时不重编）
AW_FREE_GENERATOR_FAILURE = re.compile(r"生成端调用失败：GeneratorHTTPError: HTTP (?:429|5(?!04|24)\d\d)\b")


def test_labelled_http_errors_keep_the_writer_desk_contract(monkeypatch, relays):
    with_backup(monkeypatch)
    rule = [{"id": "a", "text": "规则甲", "severity": "soft"}]
    relays.set(primary=[TIMEOUT], backup=[http_err(529)])
    out = R.compile_rules(R.make_generator(), rule)[0]["error"]
    assert "[backup]" in out and AW_FREE_GENERATOR_FAILURE.search(out), out
    assert out.startswith("生成端调用失败：GeneratorHTTPError: HTTP 529 Relay Says No [backup]：")
    for status, free in ((429, True), (503, True), (504, False), (524, False), (402, False)):
        fresh_breaker(Path(tempfile.tempdir))
        relays.set(primary=[http_err(status)], backup=[http_err(status)])
        out = R.compile_rules(R.make_generator(), rule)[0]["error"]
        assert "[backup]" in out and bool(AW_FREE_GENERATOR_FAILURE.search(out)) is free, (status, out)
    fresh_breaker(Path(tempfile.tempdir))
    relays.set(primary=[http_err(400)])
    out = R.compile_rules(R.make_generator(), rule)[0]["error"]
    assert out.startswith("生成端调用失败：GeneratorHTTPError: HTTP 400 Relay Says No [primary]：")
    assert not AW_FREE_GENERATOR_FAILURE.search(out) and PKEY not in out and BKEY not in out


def test_stop_reason_refusal_is_not_a_transport_failure(monkeypatch, relays):
    with_backup(monkeypatch)
    relays.set(primary=[ok("", stop="refusal")])
    g = R.make_generator()
    with pytest.raises(L.GeneratorStopped):
        g("p")
    assert relays.targets() == ["primary"] and relay.counts()["primary_ok"] == 1 and g.calls == 1


def test_breaker_skips_primary_then_probe_closes(monkeypatch, relays, caplog):
    with_backup(monkeypatch)
    from fastapi.testclient import TestClient
    from judge import api as A
    tc = TestClient(A.app)
    relays.set(primary=[TIMEOUT], backup=[ok("B")])
    g = R.make_generator()
    assert g("p") == ["B"] and g("p") == ["B"]
    assert relay_lines(caplog, OPEN_LINE) == [
        "relay: breaker open for 1200s after 2 consecutive primary failures (last: TimeoutError); calls go straight to backup"]
    relays.calls.clear()
    assert R.make_generator()("p") == ["B"] and relays.targets() == ["backup"]                 # 新请求的新实例也跳过主站
    assert relay.counts()["breaker_open_skips"] == 1
    assert tc.get("/health").json()["relay"]["relay_breaker"] == "open"
    later = time.time() + 1201
    monkeypatch.setattr(relay, "_now", lambda: later)
    assert tc.get("/health").json()["relay"]["relay_breaker"] == "half_open"
    relays.set(primary=[ok("P-AGAIN")])
    relays.calls.clear()
    assert g("p") == ["P-AGAIN"] and relays.targets() == ["primary"]
    assert relay_lines(caplog, "breaker closed") == ["relay: breaker closed (primary ok again)"]
    assert tc.get("/health").json()["relay"]["relay_breaker"] == "closed"


def test_relay_force_backup_and_unconfigured_backup(monkeypatch, relays, tmp_path, caplog):
    with_backup(monkeypatch, force="backup")
    relays.set(primary=[TIMEOUT], backup=[ok("ONLY-B")])
    g = R.make_generator()
    assert g._relay_mode == relay.MODE_BACKUP_ONLY and g("p") == ["ONLY-B"] and g("p") == ["ONLY-B"]
    assert relays.targets() == ["backup", "backup"] and {c["timeout"] for c in relays.calls} == {90.0}
    assert breaker_files(tmp_path) == []                                                   # backup_only 不碰熔断
    # 备用站没配却设了 backup：走老路，提示一行
    monkeypatch.delenv("ANTHROPIC_API_KEY_BACKUP")
    relays.calls.clear()
    relays.set(primary=[ok("LEGACY")])
    g2 = R.make_generator()
    assert g2._relay_mode == relay.MODE_PRIMARY_ONLY and g2("p") == ["LEGACY"] and relays.targets() == ["primary"]
    R.make_generator()
    assert relay_lines(caplog, "RELAY_FORCE") == ["relay: RELAY_FORCE=backup but no backup relay is configured; using primary"]


def test_sanitized_transport_errors_never_carry_a_host(monkeypatch, relays):
    with_backup(monkeypatch)
    ssl_err = ssl.SSLCertVerificationError(1, f"certificate verify failed: Hostname mismatch, certificate is not valid for '{PHOST}'")
    relays.set(primary=[("raise", lambda: urllib.error.URLError(ssl_err))], backup=[REFUSED])
    g = R.make_generator()
    with pytest.raises(urllib.error.URLError) as ei:
        g("p")
    assert str(ei.value) == "<urlopen error backup: ConnectionRefusedError>"
    assert relays.targets() == ["primary", "backup", "primary", "backup"]                  # 第二次尝试时熔断刚好打开
    out = R.compile_rules(g, [{"id": "a", "text": "规则甲", "severity": "soft"}])[0]["error"]
    assert "生成端调用失败" in out and "backup: ConnectionRefusedError" in out and not any(s in out for s in SECRETS)
    relays.set(primary=[TIMEOUT], backup=[TIMEOUT])
    fresh_breaker(Path(tempfile.tempdir))
    with pytest.raises(TimeoutError) as ei:
        R.make_generator()("p")
    assert str(ei.value) == "backup: timed out"
    relays.set(primary=[TIMEOUT], backup=[INCOMPLETE])
    with pytest.raises(ConnectionError) as ei:
        R.make_generator()("p")
    assert str(ei.value) == "backup: IncompleteRead"
    relays.set(primary=[TIMEOUT], backup=[("raise", lambda: urllib.error.URLError(f"unknown url type: {BHOST}"))])
    with pytest.raises(urllib.error.URLError) as ei:
        R.make_generator()("p")
    assert str(ei.value) == "<urlopen error backup: URLError>"


def test_backup_url_without_scheme_never_leaks_into_rule_errors(monkeypatch, relays):
    """owner 把备用站地址贴成不带 https:// 的：urllib 的 ValueError 原文带整个地址，不能进 rules[].error / 502 detail。"""
    with_backup(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL_BACKUP", f"{BHOST}/v1")
    relays.set(primary=[TIMEOUT])
    g = R.make_generator()
    assert g._relay_mode == relay.MODE_AUTO
    out = R.compile_rules(g, [{"id": "a", "text": "规则甲", "severity": "soft"}])[0]["error"]
    assert out == "生成端调用失败：ValueError: backup: invalid base URL (ValueError)", out
    assert relays.targets() == ["primary"] and relay.counts()["backup_fail"] == 1                # 地址写错不重试
    # 代理隧道之类在 urlopen 里抛的 ValueError 同样只留标签
    with_backup(monkeypatch)
    fresh_breaker(Path(tempfile.tempdir))
    relays.set(primary=[TIMEOUT], backup=[("raise", lambda: ValueError(f"Tunnel host can't contain control characters '{BHOST}'"))])
    with pytest.raises(ValueError) as ei:
        R.make_generator()("p")
    assert str(ei.value) == "backup: invalid base URL (ValueError)"
    # 返回体不是 JSON 不算「地址写错」：主站这样切备用站（上面 html_200），备用站这样照旧抛 JSONDecodeError、不重试
    fresh_breaker(Path(tempfile.tempdir))
    relays.calls.clear()
    relays.set(primary=[TIMEOUT], backup=[HTML200])
    with pytest.raises(json.JSONDecodeError):
        R.make_generator()("p")
    assert relays.targets() == ["primary", "backup"]


# ══ HTTP：/compile_rules 端到端 + /health ════════════════════════════════════

def _not_checkable(rid):
    return {"id": rid, "verdict": "not_checkable", "code": None, "questions": [], "why": "合成", "coverage": None, "uncovered": ""}


def test_compile_rules_end_to_end_on_backup_with_parallel_workers(monkeypatch, relays, caplog, tmp_path):
    with_backup(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "k-admin")
    monkeypatch.setenv("JUDGE_COMPILE_BATCH", "2")
    monkeypatch.setenv("JUDGE_COMPILE_WORKERS", "6")

    def compile_answer(req):
        prompt = json.loads(req.data.decode("utf-8"))["messages"][0]["content"]
        return ok(json.dumps({"results": [_not_checkable(r["id"]) for r in R.rules_in_prompt(prompt)]}, ensure_ascii=False))

    relays.set(primary=[TIMEOUT], backup=[(compile_answer,)])
    from fastapi.testclient import TestClient
    from judge import api as A
    tc = TestClient(A.app)
    rules = [{"id": f"r{i}", "text": f"合成规则{i}", "severity": "soft"} for i in range(12)]
    r = tc.post("/compile_rules", json={"project": "ANY", "rules": rules}, headers={"X-Judge-Key": "k-admin"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["errors"] == 0 and not any(x.get("error") for x in d["rules"]) and d["calls"] == 6
    assert relays.targets().count("backup") == 6 and 2 <= relays.targets().count("primary") <= 6
    assert not any(s in r.text for s in SECRETS)
    st = load_breaker()
    assert st["opened_at"] is not None and st["fails"] >= 2 and len(relay_lines(caplog, OPEN_LINE)) == 1
    assert not glob.glob(str(tmp_path / "*.tmp"))
    h = tc.get("/health").json()["relay"]
    assert h["relay_breaker"] == "open" and h["relay_counts"]["backup_ok"] == 6


def test_health_relay_block(monkeypatch):
    from fastapi.testclient import TestClient
    from judge import api as A
    tc = TestClient(A.app)
    r = tc.get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d["relay"] == {"relay_mode": "primary_only", "relay_backup_configured": False, "relay_force": "auto",
                          "relay_breaker": "off", "relay_counts": dict.fromkeys(relay.COUNTER_KEYS, 0)}
    assert d["compile"] == {"model": "claude-sonnet-5-5", "key": True}
    with_backup(monkeypatch)
    r = tc.get("/health")
    d = r.json()
    rl = d["relay"]
    assert set(rl) == {"relay_mode", "relay_backup_configured", "relay_force", "relay_breaker", "relay_counts"}
    assert rl["relay_mode"] == "auto" and rl["relay_backup_configured"] is True and rl["relay_force"] == "auto"
    assert rl["relay_breaker"] == "closed" and set(rl["relay_counts"]) == set(relay.COUNTER_KEYS)
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in rl["relay_counts"].values())
    assert d["compile"] == {"model": "claude-sonnet-5-5", "key": True}
    assert not any(s in r.text for s in SECRETS)
    # relay 出错也不能让 /health 500
    monkeypatch.setattr(relay, "health", lambda **k: (_ for _ in ()).throw(RuntimeError(PHOST)))
    r = tc.get("/health")
    assert r.status_code == 200 and r.json()["relay"] == {"relay_error": "RuntimeError"} and PHOST not in r.text
