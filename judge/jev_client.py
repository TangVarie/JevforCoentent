# -*- coding: utf-8 -*-
"""Jev（TypeSafe AI）客户端：一个接口、固定模型版本、重试、确定性 mock。

密钥按顺序找：环境变量 TYPESAFE_API_KEY → TYPESAFE_KEY_FILE 指向的文件 → ~/.config/typesafe/key。
模型版本由题库指定（bank.model），钉死不用 jev-latest；换版本先重跑金标准再改题库里的 model。
限流按官方口径 250k token/秒、1,200 次/分钟；429 按 retry-after 重试，529（Jev 过载，官方文档要求退避重试）同样重试。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_BASE_URL = "https://api.typesafe.ai"
ENDPOINT = "/v1/systemone"
USER_AGENT = "bywood-judge/0.1"
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}  # 5xx 含 Cloudflare 的 52x；529 = Jev 过载


class JevError(Exception):
    """调用失败，信息给人读。"""


def find_api_key() -> Optional[str]:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    candidates = []
    if os.environ.get("TYPESAFE_KEY_FILE"):
        candidates.append(Path(os.environ["TYPESAFE_KEY_FILE"]).expanduser())
    candidates.append(Path.home() / ".config" / "typesafe" / "key")
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return None


def _retry_wait(headers, attempt: int) -> float:
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name) if headers else None
        if raw:
            try:
                return min(float(raw) * scale, 30.0)
            except ValueError:
                pass
    return min(0.5 * (2 ** attempt), 8.0)


def _explain_http(status: int, text: str) -> str:
    snippet = text.strip().replace("\n", " ")[:300]
    if status in (401, 403):
        return f"鉴权失败（HTTP {status}）：检查 TYPESAFE_API_KEY。{snippet}"
    if status == 404:
        return f"找不到接口或模型（HTTP 404）：检查题库里的 model / TYPESAFE_BASE_URL。{snippet}"
    if status in (400, 422):
        return f"请求被拒（HTTP {status}），多半是题库格式问题：{snippet}"
    if status == 429:
        return f"限流（HTTP 429），重试后仍失败：{snippet}"
    if status == 529:
        return f"Jev 过载（HTTP 529），退避重试后仍失败：{snippet}"
    return f"Jev 返回 HTTP {status}：{snippet}"


class JevClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 30.0, retries: int = 3, mock: bool = False):
        self.mock = mock
        self.api_key = api_key or (None if mock else find_api_key())
        if not self.api_key and not mock:
            raise JevError("没找到 Jev 密钥（TYPESAFE_API_KEY / TYPESAFE_KEY_FILE / ~/.config/typesafe/key）")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def call(self, body: dict) -> dict:
        """body = {"state": ..., "model": ..., "questions": {...}} → Jev 原始返回。"""
        if self.mock:
            return mock_response(body)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": USER_AGENT}
        last = "未知错误"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.base_url + ENDPOINT, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code in RETRY_STATUSES and attempt < self.retries:
                    time.sleep(_retry_wait(exc.headers, attempt))
                    continue
                raise JevError(_explain_http(exc.code, text)) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last = f"连不上 Jev：{getattr(exc, 'reason', exc)}"
                if attempt < self.retries:
                    time.sleep(_retry_wait(None, attempt))
                    continue
                raise JevError(last) from exc
            except json.JSONDecodeError as exc:
                raise JevError("Jev 返回的不是 JSON") from exc
        raise JevError(last)


def mock_response(body: dict) -> dict:
    """确定性的假结果：同一 state + 同一题目集合永远同一答案（与题目在 dict 里的先后无关）。数字没有意义，只用来测流程。"""
    seed = hashlib.sha256(json.dumps({"s": body.get("state"), "q": sorted(body.get("questions", {}))},
                                     ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    rnd = random.Random(int(seed, 16))
    answers = {}
    qs = body.get("questions", {})
    for name in sorted(qs):                 # 按题号顺序消耗随机数：调用方换了题目插入顺序，答案不变
        q = qs[name]
        if q["type"] == "choice":
            labels = list(q["criteria"])
            weights = [rnd.random() ** 3 for _ in labels]
            total = sum(weights) or 1.0
            probs = {lab: w / total for lab, w in zip(labels, weights)}
            top = max(probs, key=probs.get)
            answers[name] = {"type": "choice", "choice": top, "confidence": probs[top], "probabilities": probs}
        else:
            answers[name] = {"type": "noul", "noul": rnd.random()}
    return {"model": "mock", "answers": answers, "usage": {"input_tokens": 0, "output_tokens": 0}}
