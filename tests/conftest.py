# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def policy_cfg(tmp_path, monkeypatch):
    """测试用的数据出境配置：OKMAN 是处方药项目、没清出境；TUGE 放行项目层；NRT 没放行项目层。"""
    from judge import policy as P
    p = tmp_path / "data_policy.yaml"
    p.write_text("rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: [TUGE]\n", encoding="utf-8")
    monkeypatch.setattr(P, "CONFIG", p)
    return p
