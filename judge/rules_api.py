# -*- coding: utf-8 -*-
"""POST /compile_rules：写作台的自由文本规则 → 写后检查（逻辑在 judge/rules.py，这里只管鉴权、数据出境和请求形状）。

只认管理 key（同 /judge）：规则原文是客户的项目层内容，写手 key 发在服务端之外的机器上，不能拿它换出境（审计 A-09 同一口径）。
数据出境：规则原文按未发布稿过 policy.decide（published=False）——处方药没清出境 403；项目层没放行 403（2026-10-20 起
config 里是 "*"，所有项目放行）。两种 403 都在调生成端之前，一次调用都不发。
"""
from __future__ import annotations

import time
from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import rules as R


class InputModel(BaseModel):
    model_config = {"extra": "forbid"}


class RuleIn(InputModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    severity: Literal["hard", "soft"]
    # 写作台 memories.applicability 原样传：部位（标题 / 正文 / 改简历部分）决定 where / scope；方向词（产品向 / 流量向）写作台自己筛。
    # 进 sha（rule_sha(text, applicability)）：适用范围改了也要重编。
    applicability: Optional[str] = Field(default=None, max_length=100)


class CompileRequest(InputModel):
    project: str = Field(min_length=1, max_length=100)
    category: Optional[str] = Field(default=None, max_length=100)
    rules: list[RuleIn] = Field(min_length=1, max_length=60)


def attach(app, *, auth, policy, taken_qids):
    """taken_qids()：本服务所有题库已有的题号（编出的题号不许撞，critic 9）。"""

    @app.post("/compile_rules", dependencies=[Depends(auth)])
    def compile_rules(req: CompileRequest):
        decision = policy(["aw_version"], req.project, req.category, False)
        if not decision.project_layer:
            raise HTTPException(403, f"policy: 项目 {decision.project} 的项目层没放行（不在 project_layer_cleared 里），"
                                     "规则原文是项目层内容、不出境（docs/00 #7）")
        ids = [r.id for r in req.rules]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise HTTPException(422, f"rules[].id 不许重复：{dup}")
        if any(not r.text.strip() for r in req.rules):
            raise HTTPException(422, "rules[].text 不能是空白")
        try:
            gen = R.make_generator()
        except R.GeneratorNotConfigured as exc:
            raise HTTPException(503, str(exc))
        taken = taken_qids()
        t0 = time.time()
        results = R.compile_rules(gen, [r.model_dump() for r in req.rules], category=req.category,
                                  batch_size=R.batch_size(), workers=R.workers(), taken=taken)
        latency_ms = round((time.time() - t0) * 1000)
        errors = sum(1 for r in results if r.get("error"))
        if errors == len(results):
            # 一条都没编出来（生成端挂了 / 输出全不合格）：同 /judge 全部失败的口径回 502，调用方稍后重试；部分失败照常 200
            raise HTTPException(502, f"规则一条都没编出来：{results[0]['error']}")
        return {"project": decision.project, "category": decision.category, "model": getattr(gen, "model", None),
                "rules": results, "project_bank": R.to_project_bank(results, taken=taken),
                "errors": errors, "calls": getattr(gen, "calls", None), "usage": dict(getattr(gen, "usage", {}) or {}),
                "latency_ms": latency_ms, "policy": decision.as_dict()}
