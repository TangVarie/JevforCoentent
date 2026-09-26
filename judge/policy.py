# -*- coding: utf-8 -*-
"""数据出境（docs/00 #7）：公开笔记直接跑；未发布稿默认只跑通用层 + 平台层；处方药项目的未发布稿不跑，直到合同确认。

HTTP（/judge、/judge_draft）和写手侧 MCP 共用这一处判断，配置在 config/data_policy.yaml：
  · 「未发布」按 subject_type 定：aw_version（写作台稿）、ssll_sample（三省六部采样稿）。note / external_note / comment 是公开内容。
  · 未发布稿必须带 project（项目代号），可再带 category（TV 统一词表）。不带 project → 422：不知道是哪个项目就判断不了能不能出境。
  · category 在 rx_categories 里、或 project 在 rx_projects 里，且 project 不在 rx_cleared → 403（PolicyBlocked），一次 Jev 调用都不发。
  · 项目层（brief 现编 / 内联 / 名字不属于通用层和平台层的题库）只有 project 在 project_layer_cleared 里才跑；
    否则整层去掉，连同指向它的硬约束，在响应的 policy.dropped_banks / dropped_hard_rules 里回显。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

CONFIG = Path(os.environ.get("JUDGE_POLICY_CONFIG", Path(__file__).resolve().parent.parent / "config" / "data_policy.yaml"))
UNPUBLISHED_SUBJECT_TYPES = frozenset({"aw_version", "ssll_sample"})
GENERIC_PREFIXES = ("feature_questions", "human_feel", "comment_", "ssll_critic", "external_triage")
PLATFORM_PREFIXES = ("platform",)


class PolicyBlocked(Exception):
    """处方药项目的未发布稿：不出境。HTTP 映射成 403，detail 以 "policy:" 开头，调用方记 policy_blocked、不重试。"""


class PolicyInputError(ValueError):
    """未发布稿没带 project 等：HTTP 映射成 422。"""


def load() -> dict:
    if not CONFIG.exists():
        return {}
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def layer_of(bank_name: str) -> str:
    """generic（通用层）/ platform（平台层）/ project（项目层）。按名字前缀，与 /judge_draft 分层同一口径。"""
    if bank_name.startswith(PLATFORM_PREFIXES):
        return "platform"
    if bank_name.startswith(GENERIC_PREFIXES):
        return "generic"
    return "project"


@dataclass
class Decision:
    published: bool
    project: Optional[str] = None
    category: Optional[str] = None
    project_layer: bool = True               # 这次能不能跑项目层
    dropped_banks: list = field(default_factory=list)
    dropped_hard_rules: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"published": self.published, "project": self.project, "category": self.category,
                "project_layer": self.project_layer, "dropped_banks": self.dropped_banks,
                "dropped_hard_rules": self.dropped_hard_rules}


def decide(subject_types: Iterable[str], project: Optional[str] = None, category: Optional[str] = None,
           cfg: Optional[dict] = None, published: Optional[bool] = None) -> Decision:
    """published=False 可以把公开类型（如预埋评论的 comment）收紧成未发布；published=True 放不宽未发布类型。"""
    cfg = load() if cfg is None else cfg
    project = (project or "").strip() or None
    category = (category or "").strip() or None
    unpublished = published is False or any(t in UNPUBLISHED_SUBJECT_TYPES for t in subject_types)
    if not unpublished:
        return Decision(published=True, project=project, category=category, project_layer=True)
    if not project:
        raise PolicyInputError("未发布稿（aw_version / ssll_sample）必须带 project（项目代号）：不知道是哪个项目就判断不了能不能出境（docs/00 #7）")
    rx = (category in set(cfg.get("rx_categories") or [])) or (project in set(cfg.get("rx_projects") or []))
    if rx and project not in set(cfg.get("rx_cleared") or []):
        raise PolicyBlocked(f"policy: 处方药项目 {project} 的未发布稿不出境，合同确认后把它加进 config/data_policy.yaml 的 rx_cleared（docs/00 #7）")
    return Decision(published=False, project=project, category=category,
                    project_layer=project in set(cfg.get("project_layer_cleared") or []))
