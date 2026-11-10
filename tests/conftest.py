# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_RELAY_ENV = ("ANTHROPIC_BASE_URL_BACKUP", "ANTHROPIC_API_KEY_BACKUP", "RELAY_FORCE", "RELAY_PRIMARY_TIMEOUT_S", "RELAY_SLOW_S",
              "RELAY_BREAKER_FAILS", "RELAY_BREAKER_COOLDOWN_S")


@pytest.fixture(autouse=True)
def _no_backup_relay_from_the_shell(monkeypatch):
    """备用中转站（D-148）：开发机 shell 里配了备用站也不带进测试——老用例照旧走「没配备用站」的原路（不写临时目录的熔断文件），
    也绝不会打到真的备用站；要测备用站的用例（tests/test_relay_backup.py）自己设 .invalid 假地址。"""
    for k in _RELAY_ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def mcp_via_testclient(monkeypatch):
    """让 MCP 薄客户端（judge/mcp_server.py）的 urllib 请求打到进程内的 FastAPI TestClient，不联网：
    路径、方法、头、body 原样转发；>=400 变成 urllib 的 HTTPError（服务端的 {"detail": …} 照带）。返回收到的 Request 列表。"""
    import io
    import urllib.error
    import urllib.request
    from fastapi.testclient import TestClient
    from judge import api as A
    monkeypatch.setenv("JUDGE_URL", "http://judge.test"); monkeypatch.setenv("JUDGE_API_KEY", "k")
    c = TestClient(A.app)
    seen: list = []

    class _Resp:
        def __init__(self, r):
            self._r, self.status = r, r.status_code

        def read(self):
            return self._r.content

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen.append(req)
        path = "/" + req.full_url.split("://", 1)[1].split("/", 1)[1]
        r = c.request(req.get_method(), path, content=req.data, headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, r.reason_phrase, r.headers, io.BytesIO(r.content))
        return _Resp(r)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


@pytest.fixture
def policy_cfg(tmp_path, monkeypatch):
    """测试用的数据出境配置：OKMAN 是处方药项目、没清出境；TUGE 放行项目层；NRT 没放行项目层。"""
    from judge import policy as P
    p = tmp_path / "data_policy.yaml"
    p.write_text("rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: [TUGE]\n", encoding="utf-8")
    monkeypatch.setattr(P, "CONFIG", p)
    return p
