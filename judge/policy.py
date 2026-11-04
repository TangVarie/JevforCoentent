# -*- coding: utf-8 -*-
"""数据出境（docs/00 #7）：公开笔记直接跑；未发布稿默认只跑通用层 + 平台层；处方药项目的未发布稿不跑，直到合同确认。

HTTP（/judge、/judge_draft）和写手侧 MCP 共用这一处判断，配置在 config/data_policy.yaml：
  · 「未发布」按 subject_type 定：aw_version（写作台稿）、ssll_sample（三省六部采样稿）。note / external_note / comment 是公开内容。
  · 未发布稿必须带 project（项目代号），可再带 category（TV 统一词表）。不带 project → 422：不知道是哪个项目就判断不了能不能出境。
  · category 在 rx_categories 里、或 project 在 rx_projects 里，且 project 不在 rx_cleared → 403（PolicyBlocked），一次 Jev 调用都不发。
  · 项目层（brief 现编 / 内联 / 名字不属于通用层和平台层的题库）只有 project 在 project_layer_cleared 里才跑；
    否则整层去掉，连同指向它的硬约束，在响应的 policy.dropped_banks / dropped_hard_rules 里回显。
    project_layer_cleared 里写 "*" = 所有项目（2026-10-20 owner 拍板「全部放行，一次到位」）；显式列项目代号照旧有效。
    "*" 只认在 project_layer_cleared 里：处方药闸（rx_projects / rx_cleared）不认通配、照旧先执行，没清出境的处方药项目照样 403。
  · POST /compile_rules（写作台的自由文本规则编成写后检查）：规则原文就是项目层内容，按未发布稿过同一套判断，项目层没放行 → 403。
  · 配置文件缺失、读不出或形状不对 → 直接抛 PolicyConfigError（2026-11-04，审计 11-02 F15）。以前缺文件时 load() 静默回 {}，
    处方药名单整个变空、处方药闸等于没装（今天唯一的处方药项目碰巧已放行，才没出事）。服务启动时先校验一遍，坏部署 /health 就失败；
    /health 回显 policy_sha256 = 这份文件字节的 sha256，和仓里 `sha256sum config/data_policy.yaml` 对得上就是同一份（J-10）。
"""
from __future__ import annotations

import hashlib
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


class PolicyConfigError(RuntimeError):
    """config/data_policy.yaml 缺失、读不出或形状不对：HTTP 映射成 503，一次 Jev 都不调。不回落成空配置（审计 11-02 F15）。"""


PROJECT_LAYER_ALL = "*"
# 四个键都必须写（空名单写 []）：少一个就是配置坏了，不能当「没有处方药项目」处理。多出来的键也算坏：
# 比如把 rx_projects 拼错成 rx_project 再补一行，新处方药项目就悄悄没进闸。
REQUIRED_KEYS = ("rx_categories", "rx_projects", "rx_cleared", "project_layer_cleared")


def _read() -> bytes:
    try:
        return CONFIG.read_bytes()
    except FileNotFoundError:
        raise PolicyConfigError(f"数据出境配置不存在：{CONFIG}（处方药闸靠它；部署要带上 config/data_policy.yaml，或用 JUDGE_POLICY_CONFIG 指对位置）") from None
    except OSError as exc:
        raise PolicyConfigError(f"数据出境配置读不了：{CONFIG}：{exc}") from None


def validate(cfg) -> dict:
    """形状校验。合格原样返回；不合格抛 PolicyConfigError，说清哪一个键错了。"""
    if not isinstance(cfg, dict):
        raise PolicyConfigError(f"数据出境配置 {CONFIG} 顶层要是映射（四个键：{', '.join(REQUIRED_KEYS)}），读到的是 {type(cfg).__name__}")
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    unknown = sorted(str(k) for k in cfg if k not in REQUIRED_KEYS)
    if missing or unknown:
        raise PolicyConfigError(f"数据出境配置 {CONFIG} 的键不对：缺 {missing}，多出 {unknown}（四个键都要写，空名单写 []）")
    for k in REQUIRED_KEYS:
        v = cfg[k]
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise PolicyConfigError(f"数据出境配置 {CONFIG} 的 {k} 要是字符串列表（例：[OKMAN, OKMAN_phase1]），读到的是 {v!r}")
        if k != "project_layer_cleared" and PROJECT_LAYER_ALL in {x.strip() for x in v}:
            raise PolicyConfigError(f"数据出境配置 {CONFIG} 的 {k} 里不能写 \"*\"：通配只在 project_layer_cleared 认，处方药闸要逐个写项目代号")
    return cfg


def parse(raw: bytes) -> dict:
    try:
        cfg = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PolicyConfigError(f"数据出境配置 {CONFIG} 读不出：{exc}（YAML 里 * 要带引号写成 \"*\"）") from None
    return validate(cfg)


def load() -> dict:
    """读并校验 config/data_policy.yaml。缺文件、YAML 坏、形状不对都抛 PolicyConfigError，不再静默回 {}。"""
    return parse(_read())


def config_sha256() -> str:
    """校验过的配置文件字节的 sha256（/health 回显，J-10）。配置坏了照样抛，不给坏文件算指纹。"""
    raw = _read()
    parse(raw)
    return hashlib.sha256(raw).hexdigest()


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
                    project_layer=project_layer_cleared(project, cfg))


def project_layer_cleared(project: Optional[str], cfg: Optional[dict] = None) -> bool:
    """这个项目的未发布稿能不能跑项目层。"*" = 所有项目（10-20 owner 拍板全部放行）；否则看显式名单。
    为什么做成通配而不是把现有项目代号逐个列上：新项目上线（如 10-19 的 XIWU_phase2）不该再等一次改配置才有项目层，
    owner 的原话是「全部放行，一次到位」。处方药闸在 decide 里先执行，这里不管。"""
    cfg = load() if cfg is None else cfg
    cleared = {str(x).strip() for x in (cfg.get("project_layer_cleared") or [])}
    return PROJECT_LAYER_ALL in cleared or (project or "") in cleared
