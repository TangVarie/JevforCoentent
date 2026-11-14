# -*- coding: utf-8 -*-
"""外部语料：定时、限额地从公开平台抓「对飞轮有用」的笔记，打标入账本。

流程（每次运行）：
  读 config → 每个品类 × 关键词 × 排序 × 页：搜索（每页计一次费）→ 去重（本地 state + 账本已有的 note_id）
  → 分诊（Jev，四题，只看搜索页给的标题 / 摘要；不花抓取费）→ 留下的取全文（每条计一次费）
  → fq 题库打标（两次 Jev 调用）→ 外部笔记表 + 账本行（subject_type = external_note）→ 报告。
硬约束：预算到了就停；每品类每次 / 每月上限；互动数只进 external_notes 表，不进任何 Jev 的 state（Mode A）。

供应商：TikHub（GET /api/v1/xiaohongshu/app_v2/search_notes，Bearer；每次请求 0.01 美元；默认 10 次/秒）。
返回字段没有公开 schema，_find_notes / _pick 做防御性取值，第一次联网先 --probe 把原始 JSON 看一遍再钉。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .banks import INVALID_MISSING, INVALID_OUT_OF_VOCAB, Bank
from .core import judge_note, judge_state, ledger_rows
from .jev_client import JevClient

TIKHUB_BASE = os.environ.get("TIKHUB_BASE_URL", "https://api.tikhub.io")
SEARCH_PATH = "/api/v1/xiaohongshu/app_v2/search_notes"
DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"   # 官方文档：图文接口图文、视频笔记都能取到文字；视频接口只认视频
USER_AGENT = "bywood-judge/0.1 (+https://github.com/TangVarie/JevforCoentent)"
DETAIL_PROBE = 5           # 连着这么多篇取全文失败（接口报错或解析不出正文）→ 整次停，不再按篇白付费
DETAIL_SAMPLES = 3         # 每次运行留几份详情脱敏结构进产物，字段变了能直接看（不留正文/账号）
SEARCH_SAMPLES = 1         # 每次运行留几份「一条笔记都没认出来」的搜索原始返回进产物（脱敏），格式变了能直接看
# Jev 回了 200 却没答（missing）或答案不在选项里（out_of_vocab）：这篇「没判成」，不是「判了是否」。
# 设计内的无效（no_title / text_too_short / evidence_not_found / span_truncated）是判完了的结论，照常入账本。
JEV_INCOMPLETE = frozenset({INVALID_MISSING, INVALID_OUT_OF_VOCAB})


class BudgetExceeded(Exception):
    pass


class DetailFailed(RuntimeError):
    """取全文失败：请求报错（401 / 5xx / 超时）或返回里没有正文。多半照样计费，所以连续失败要整次停。"""


class DetailUnparseable(DetailFailed):
    """详情接口返回里找不到正文（上游「服务异常」或返回格式变了）。"""


class DetailEndpointDown(RuntimeError):
    pass


class SearchUnparseable(RuntimeError):
    """搜索接口 HTTP 200、一条笔记都没认出来，且业务码报错（顶层 code ≠ 200，或里层 success=false / code 不是 0 或 200）。这次请求已计费。"""


class JevIncomplete(RuntimeError):
    """Jev 返回里缺题或答案不在选项里（invalid_reason = missing / out_of_vocab）：这篇没判成，不进 seen、不入账本，下次重判。"""


def _incomplete(items: dict, qids: Optional[tuple] = None) -> list:
    """没判成的题：["qid=missing", ...]（按题号排）。qids 给了就只看这几题。"""
    return sorted(f"{q}={it['invalid_reason']}" for q, it in items.items()
                  if (qids is None or q in qids) and (it or {}).get("invalid_reason") in JEV_INCOMPLETE)


@dataclass
class Budget:
    limit_usd: float
    price_per_call: float = 0.01
    calls: int = 0

    @property
    def spent(self) -> float:
        return round(self.calls * self.price_per_call, 4)

    def charge(self, n: int = 1) -> None:
        if (self.calls + n) * self.price_per_call > self.limit_usd + 1e-9:
            raise BudgetExceeded(f"预算 {self.limit_usd} 美元用完（已 {self.calls} 次）")
        self.calls += n


# ── 供应商 ──────────────────────────────────────────────────────────────

def _pick(d: Any, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _find_notes(obj: Any, depth: int = 0) -> list:
    """在返回 JSON 里找「笔记列表」：一个 list，元素是带 note_id/id 且带 title/desc 的 dict。"""
    if depth > 6:
        return []
    if isinstance(obj, list):
        def is_note(x):
            if not isinstance(x, dict):
                return False
            card = _card(x)
            return _pick(card, "note_id", "id") is not None and _pick(card, "title", "desc", "display_title", "content") is not None
        hits = [x for x in obj if is_note(x)]
        if hits:
            return hits                     # 只回认得出的笔记：列表里混进的广告位 / 非 dict 元素不再让 normalize_note 崩
        for x in obj:
            r = _find_notes(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for k in ("items", "notes", "note_list", "data", "result", "list"):
            if k in obj:
                r = _find_notes(obj[k], depth + 1)
                if r:
                    return r
        for v in obj.values():
            r = _find_notes(v, depth + 1)
            if r:
                return r
    return []


def _card(x: dict) -> dict:
    """笔记本体可能包在 note_card（web 端）或 note（app_v2 的 items[].note）里；都不是就是它自己。"""
    for k in ("note_card", "note"):
        if isinstance(x.get(k), dict):
            return x[k]
    return x


def normalize_note(n: dict, keyword: str, category: str, sort_type: str) -> dict:
    card = _card(n)
    user = _pick(card, "user", "author", default={}) or {}
    inter = _pick(card, "interact_info", "interactions", default={}) or {}
    def num(*ks):
        v = _pick(inter, *ks) if inter else None
        if v is None:
            v = _pick(card, *ks, default=0)
        t = str(v).replace(",", "").replace("+", "").strip()
        try:
            if t.endswith("万"):
                return int(float(t[:-1]) * 10000)
            if t.endswith("w") or t.endswith("W"):
                return int(float(t[:-1]) * 10000)
            return int(float(t or 0))
        except ValueError:
            return 0
    return {"note_id": str(_pick(card, "note_id", "id", default="")), "keyword": keyword, "category": category, "sort_type": sort_type,
            "title": str(_pick(card, "title", "display_title", default="") or ""), "body": str(_pick(card, "desc", "content", "body", default="") or ""),
            "note_type": str(_pick(card, "type", "note_type", default="") or ""),
            "author": str(_pick(user, "nickname", "nick_name", "name", default="") or ""),
            "author_id": str(_pick(user, "user_id", "id", default="") or ""),
            "liked": num("liked_count", "like_count", "likes"), "collected": num("collected_count", "collect_count"),
            "comments": num("comment_count", "comments_count"), "shares": num("shared_count", "share_count"),
            "publish_time": _pick(card, "time", "publish_time", "create_time"), "raw": n}


TEXT_KEYS = ("desc", "content", "note_text")


def _detail_text(raw: Any, note_id: str) -> tuple:
    """详情返回里这篇笔记的（标题, 正文）。没有公开 schema，所以整棵树找带 desc / content 的 dict：id 对得上的优先，其次正文最长的。
    10-08 实跑确认：正文在 data.data[0].note_list[0].desc（同层 id = 本篇）；同层的 share_info.content、mini_program_info.desc
    是截到 60 字的分享文案、不带 id，靠 id 优先才不会拿错。找不到返回 ("", "")。"""
    cands = []

    def walk(x: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(x, dict):
            body = next((x[k] for k in TEXT_KEYS if isinstance(x.get(k), str) and x[k].strip()), "")
            if body:
                own = str(_pick(x, "note_id", "id", default="") or "") == note_id
                cands.append((own, len(body), str(_pick(x, "title", "display_title", default="") or ""), body))
            for v in x.values():
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:50]:
                walk(v, depth + 1)

    walk(raw, 0)
    if not cands:
        return "", ""
    _, _, title, body = max(cands, key=lambda c: (c[0], c[1]))
    return title, body


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _search_business_error(raw: Any) -> str:
    """搜索返回 HTTP 200 但业务上报错时说明哪里错；没报错返回 ""。只在这页一条笔记都没认出来时才查（认得出笔记就照常用）。
    顶层 code 有值且不是 200、或 success=false；里层 data.success=false、或 data 没说 success 而 data.code 不是 0 / 200。
    （TikHub 成功时是 {code: 200, data: {code: 0, success: true, data: {items…}}}；详情接口已见过 code 200 外壳包着上游失败。）"""
    if not isinstance(raw, dict):
        return "返回不是 JSON 对象"
    if raw.get("code") is not None and _as_int(raw["code"]) != 200:
        return f"code={raw['code']}"
    if raw.get("success") is False:
        return "success=false"
    data = raw.get("data")
    if isinstance(data, dict):
        if data.get("success") is False:
            return "data.success=false"
        if data.get("success") is not True and data.get("code") is not None and _as_int(data["code"]) not in (0, 200):
            return f"data.code={data['code']}"
    return ""


# 只认整个键名或常见后缀：裸子串会误伤 author（含 auth）、design（含 sign）这类字段，整块换成 ***，形状漂移时正好丢掉要看的结构
_SECRETISH = re.compile(r"^(auth|authorization|proxy[_-]?authorization|cookies?|set[_-]?cookie|sign|key)$"
                        r"|(token|secret|signature|passw(or)?d|api[_-]?key|access[_-]?key|secret[_-]?key)$", re.I)


def _masked(obj: Any, api_key: str = "", depth: int = 0) -> Any:
    """原始返回脱敏后再进报错信息 / 产物：像密钥的字段值换成 ***，回显里的 API key 也换掉。"""
    if depth > 12:
        return "…"
    if isinstance(obj, dict):
        return {k: ("***" if _SECRETISH.search(str(k)) and obj[k] not in (None, "") else _masked(obj[k], api_key, depth + 1)) for k in obj}
    if isinstance(obj, list):
        return [_masked(x, api_key, depth + 1) for x in obj]
    if isinstance(obj, str) and api_key and api_key in obj:
        return obj.replace(api_key, "***")
    return obj


def _detail_diagnostic(raw: Any, api_key: str = "") -> Any:
    """详情诊断只留结构和状态值；先脱敏，再缩减，正文、昵称和未知字段的值不进诊断产物。"""
    safe = _masked(raw, api_key)

    def shape(obj: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 8:
            return "…"
        if isinstance(obj, dict):
            return {k: shape(v, str(k), depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [shape(v, depth=depth + 1) for v in obj[:3]]
        if obj is None or isinstance(obj, bool):
            return obj
        if key in {"code", "status", "status_code"} and isinstance(obj, (int, float)):
            return obj
        if obj == "***":
            return obj
        if isinstance(obj, str):
            return {"type": "str", "chars": len(obj)}
        return {"type": type(obj).__name__}

    return shape(safe)


def _find_session(obj: Any, depth: int = 0) -> dict:
    """搜索返回里的翻页会话：TikHub 要求第 2 页起回传首页给的 search_id / search_session_id（官方 OpenAPI 的「翻页说明」）。"""
    if depth > 5:
        return {}
    if isinstance(obj, dict):
        got = {k: str(obj[k]) for k in ("search_id", "search_session_id") if isinstance(obj.get(k), (str, int)) and obj[k] not in ("", 0)}
        if got:
            return got
        for v in obj.values():
            r = _find_session(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj[:5]:
            r = _find_session(v, depth + 1)
            if r:
                return r
    return {}


class TikHubClient:
    def __init__(self, api_key: Optional[str] = None, budget: Optional[Budget] = None, rps: float = 5.0, mock: bool = False):
        self.api_key = api_key or os.environ.get("TIKHUB_API_KEY", "")
        self.budget = budget or Budget(limit_usd=1.0)
        self.min_interval = 1.0 / max(rps, 0.1)
        self._last = 0.0
        self.mock = mock
        self._sessions: dict = {}          # (keyword, sort_type) → {search_id, search_session_id}，翻页时回传
        self.detail_samples: list = []     # 前 DETAIL_SAMPLES 份详情原始返回，进产物
        self.search_samples: list = []     # 前 SEARCH_SAMPLES 份「一条笔记都没认出来」的搜索返回（脱敏），进产物
        if not self.api_key and not mock:
            raise RuntimeError("没有 TIKHUB_API_KEY")

    def _get(self, path: str, params: dict) -> dict:
        self.budget.charge(1)
        if self.mock:
            return mock_search(params) if "search" in path else mock_detail(params)
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{TIKHUB_BASE}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        # TikHub 前面的 Cloudflare 按 UA 封 Python-urllib（Error 1010，403），必须自带 UA
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "User-Agent": USER_AGENT})
        self._last = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def has_session(self, keyword: str, sort_type: str) -> bool:
        return (keyword, sort_type) in self._sessions

    def search(self, keyword: str, page: int, sort_type: str, note_type: Optional[str]) -> tuple:
        params = {"keyword": keyword, "page": page, "sort_type": sort_type, "note_type": note_type}
        sess = self._sessions.get((keyword, sort_type))
        if page > 1 and sess:
            params.update(sess)
        raw = self._get(SEARCH_PATH, params)
        notes = _find_notes(raw)
        if not notes:
            self._keep_search_sample(keyword, page, sort_type, raw)
            # 业务码只在一条笔记都没认出来时才算数：真实外壳的里层 code 没钉过，认得出笔记的页不能因为它被扔掉
            bad = _search_business_error(raw)
            if bad:                                  # HTTP 200 的业务报错：当这页搜索失败（上层记错、不翻后面的页、全失败就红）
                raise SearchUnparseable(f"搜索业务报错（{bad}）：{json.dumps(_masked(raw, self.api_key), ensure_ascii=False)[:300]}")
        if not sess:
            found = _find_session(raw)
            if found:
                self._sessions[(keyword, sort_type)] = found
        return notes, raw

    def _keep_search_sample(self, keyword: str, page: int, sort_type: str, raw: Any) -> None:
        if len(self.search_samples) < SEARCH_SAMPLES:
            self.search_samples.append({"keyword": keyword, "page": page, "sort_type": sort_type, "raw": _masked(raw, self.api_key)})

    def detail(self, note_id: str, note_type: str = "") -> tuple:
        """返回 ({title, desc}, 原始返回)；解析不出正文抛 DetailUnparseable（这次请求已计费）。"""
        raw = self._get(DETAIL_PATH, {"note_id": note_id})
        diagnostic = _detail_diagnostic(raw, self.api_key)
        if len(self.detail_samples) < DETAIL_SAMPLES:
            self.detail_samples.append({"note_id": note_id, "raw": diagnostic})
        title, body = _detail_text(raw, note_id)
        if not body:
            raise DetailUnparseable(f"详情里找不到正文（返回结构）：{json.dumps(diagnostic, ensure_ascii=False)[:300]}")
        return {"title": title, "desc": body}, raw


# ── mock 供应商（测试与 dry-run 用，不联网、不花钱）────────────────────────

def mock_search(params: dict) -> dict:
    kw, page, sort_type = params.get("keyword", "x"), int(params.get("page", 1)), params.get("sort_type", "general")
    base = 1000 if sort_type == "popularity_descending" else 30
    notes = []
    for i in range(20):
        import hashlib
        nid = "m" + hashlib.sha256(f"{kw}|{sort_type}|{page}|{i}".encode("utf-8")).hexdigest()[:10]
        notes.append({"note_id": nid, "title": f"{kw}第{page * 20 + i}天，说说感受", "desc": f"关于{kw}的一条摘要，讲了自己昨天在药店的经历，有点想问大家。",
                      "user": {"nickname": f"用户{i}"}, "interact_info": {"liked_count": base - i * 3, "collected_count": i, "comment_count": i % 7}, "type": "normal"})
    # 假供应商也给翻页会话，并把本次请求带没带会话参数原样回显（测试用）
    return {"code": 200, "data": {"items": notes, "search_id": f"sid-{kw}-{sort_type}", "search_session_id": f"ssid-{kw}-{sort_type}"},
            "echo": {"search_id": params.get("search_id"), "search_session_id": params.get("search_session_id"), "page": page}}


def mock_detail(params: dict) -> dict:
    return {"code": 200, "data": {"note": {"note_id": params.get("note_id"), "title": "全文标题",
            "desc": "早起第12天，写两句。昨天在便利店看到无糖茶就买了一瓶试试。喝了几口有点苦，不确定能坚持多久。有几次忘了定闹钟，睡过头还是挺懊恼的。大家试过吗？"}}}


# ── 一次运行 ──────────────────────────────────────────────────────────────

@dataclass
class RunReport:
    started: str
    calls: int = 0
    spent_usd: float = 0.0
    searched: int = 0
    candidates: int = 0
    duplicates: int = 0
    triaged_kept: int = 0          # 分诊通过（还没取全文 / 打标）
    fetched: int = 0
    dropped_short: int = 0         # 分诊通过、取了全文仍太短而丢弃
    judged: int = 0                # 打标入账本
    per_category: dict = field(default_factory=dict)
    stopped_reason: str = ""
    errors: list = field(default_factory=list)      # [(哪一步, 错误)]：单条失败不中止整次运行
    search_attempts: int = 0       # 发出的搜索请求数（含失败的）；searched 只数成功的
    processed: int = 0             # 走进分诊 / 取全文 / 打标的笔记数（含失败的）
    note_errors: int = 0
    detail_failed: int = 0         # 取全文失败的篇数（请求报错或解析不出正文；计在 note_errors 里）
    detail_samples: list = field(default_factory=list)
    search_samples: list = field(default_factory=list)
    warnings: list = field(default_factory=list)    # 不至于红、但要人看一眼的（某品类搜了几页一条笔记都没认出来）
    systemic_failure: str = ""     # 全部搜索或全部笔记都失败：供应商 / Jev 挂了或密钥失效，脚本要让 job 红
    kept_notes: list = field(default_factory=list)
    rows: list = field(default_factory=list)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen": {}, "monthly": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def triage_state(n: dict, category: str) -> dict:
    return {"说明": "下面是小红书搜索结果页上的一条笔记，只有标题、摘要和作者昵称。只根据文字判断。",
            "品类": category, "关键词": n["keyword"], "标题": n["title"] or "（无）", "摘要": n["body"][:400] or "（无）", "作者昵称": n["author"] or "（无）"}


def _process_note(n: dict, name: str, cfg: dict, provider: TikHubClient, jev: JevClient, triage_bank: Bank, fq_bank: Bank,
                  rep: RunReport, stats: dict, keep_voices: set, min_chars: int) -> str:
    """一条候选从分诊到入账本。返回结局：triage_reject / short / kept。抛 BudgetExceeded 由上层停整次运行；其它异常由上层记错继续。
    Jev 缺答 / 答案不在选项里 → JevIncomplete（上层记错、不进 seen，下次重判）：不能把「没判成」当成「分诊拒绝」或「判完了」。"""
    tr = judge_state(jev, triage_bank, n["note_id"], triage_state(n, name), subject_type="external_note")
    it = tr.items
    on_topic, voice = it.get("on_topic") or {}, it.get("voice") or {}
    # 判成了的那一题已经足以拒绝（on_topic 判成不是「是」，或 voice 判成不留的视角）就是拒绝，另一题缺不缺都一样
    if ((on_topic.get("invalid_reason") not in JEV_INCOMPLETE and on_topic.get("answer") != "是")
            or (voice.get("invalid_reason") not in JEV_INCOMPLETE and voice.get("answer") not in keep_voices)):
        return "triage_reject"
    bad = _incomplete(it, ("on_topic", "voice"))
    if bad:                                             # 判成的那题说「留」、另一题没判成：这篇没判成，下次重判
        raise JevIncomplete(f"分诊没判成（{', '.join(bad)}）")
    bad = _incomplete(it)                               # 留下的四题都要入账本 / 外部笔记表：取全文（付费）之前就查
    if bad:
        raise JevIncomplete(f"分诊没判成（{', '.join(bad)}）")
    n["triage"] = {k: v["answer"] for k, v in it.items()}
    # 一律取全文：搜索页的摘要被 TikHub 截在 60 字，拿摘要打 fq 等于只看半句话（10-08 首跑 200 篇全是 60 字片段）
    stats["detail_calls"] += 1                          # 每品类每次的全文上限按请求数算（含失败 / 太短的），plan() 的最坏花费才成立
    try:
        full, _ = provider.detail(n["note_id"], n["note_type"])
    except (BudgetExceeded, DetailFailed):
        raise
    except Exception as exc:  # noqa: BLE001 — 请求层的错（401 / 5xx / 超时）也算取全文失败，参与连续失败计数
        raise DetailFailed(f"取全文失败：{type(exc).__name__}: {exc}") from exc
    rep.fetched += 1; stats["fetched"] += 1
    n["title"] = str(full.get("title") or n["title"])
    n["body"] = str(full["desc"])
    rep.triaged_kept += 1; stats["triaged"] += 1        # 取全文之后才计，预算在取全文时用完的那条不算「分诊通过」，报告四列对得上
    if len(n["body"]) < min_chars:
        rep.dropped_short += 1; stats["short"] += 1
        return "short"
    raw_text = f"标题：{n['title']}\n正文：{n['body']}" if n["title"] else n["body"]
    jr = judge_note(jev, fq_bank, n["note_id"], raw_text, title_extraction="markers", subject_type="external_note")
    bad = _incomplete(jr.items)
    if bad:                                             # 全文这次已付费；下次重取一次（0.01 美元），总比账本里留一篇缺题的、被 known_ids 当成写齐强
        raise JevIncomplete(f"fq 打标没判成 {len(bad)} 题（{', '.join(bad[:5])}）")
    rep.rows.extend(ledger_rows(jr, fq_bank, run_tag="external"))
    rep.rows.extend(ledger_rows(tr, triage_bank, run_tag="external"))
    n["fq"] = {k: v.get("answer") for k, v in jr.items.items()}
    n.pop("raw", None)
    rep.kept_notes.append(n); rep.judged += 1
    stats["kept"] += 1; stats["judged"] += 1
    return "kept"


def run_once(cfg: dict, provider: TikHubClient, jev: JevClient, triage_bank: Bank, fq_bank: Bank, state: dict,
             known_ids: Optional[set] = None, dry_run: bool = False) -> RunReport:
    """一次运行。纪律：
      · seen 只在一条笔记有了结局（分诊拒绝 / 太短 / 入账本）之后才写；因上限、预算、报错而没看的不写，下次还会看。
      · dry_run 不写 seen、不调 Jev、不取全文，只数候选。
      · 每次 / 每月上限到了整个品类停搜（不再逐页付费）；预算到了整次运行停。
      · 单条笔记或单页搜索的异常记进 rep.errors 继续跑；意外异常也不丢产物，写进 stopped_reason。"""
    rep = RunReport(started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    month = rep.started[:7]
    keep_voices = set(cfg.get("keep_voices") or ["普通用户"])
    per_run_cap = int(cfg.get("max_keep_per_category_per_run", 40))
    per_month_cap = int(cfg.get("max_keep_per_category_per_month", 160))
    min_chars = int(cfg.get("min_body_chars", 60))
    seen = state.setdefault("seen", {})
    monthly = state.setdefault("monthly", {}).setdefault(month, {})
    known_ids = known_ids or set()

    def err(where: str, exc: Exception, stats: dict) -> None:
        rep.errors.append((where, f"{type(exc).__name__}: {exc}"))
        stats["errors"] += 1

    detail_streak = 0                                   # 连续取全文失败的篇数；有一篇取到就清零
    try:
        for cat in cfg.get("categories") or []:
            name = cat["name"]
            stats = rep.per_category.setdefault(name, {"searched": 0, "candidates": 0, "triaged": 0, "kept": 0, "fetched": 0,
                                                       "short": 0, "judged": 0, "errors": 0, "detail_calls": 0})
            if monthly.get(name, 0) >= per_month_cap:
                stats["note"] = "本月上限已到"; continue

            def cap_reached() -> bool:
                return stats["detail_calls"] >= per_run_cap or monthly.get(name, 0) >= per_month_cap

            done = False
            for kw in cat.get("keywords") or []:
                for sort_type in cfg.get("sorts") or ["general"]:
                    for page in range(1, int(cfg.get("pages_per_sort", 1)) + 1):
                        if cap_reached():
                            done = True; break
                        if page > 1 and not provider.has_session(kw, sort_type):
                            # 没有首页给的翻页会话就不花钱翻页：TikHub 会把它当新搜索返回首页等价结果
                            err(f"search {kw}/{sort_type}/p{page}", RuntimeError("首页没有返回 search_id / search_session_id，跳过翻页"), stats); break
                        rep.search_attempts += 1
                        try:
                            notes, _ = provider.search(kw, page, sort_type, cfg.get("note_type"))
                        except BudgetExceeded:
                            raise
                        except Exception as exc:  # noqa: BLE001 — 供应商一页失败不中止整次运行；这个关键词×排序余下的页也不翻
                            err(f"search {kw}/{sort_type}/p{page}", exc, stats); break
                        rep.searched += 1; stats["searched"] += 1
                        if not notes:
                            break                                       # 这页一条笔记都没认出来：后面的页也不花钱翻
                        for raw in notes:
                            if not isinstance(raw, dict):
                                continue
                            n = normalize_note(raw, kw, name, sort_type)
                            if not n["note_id"]:
                                continue
                            rep.candidates += 1; stats["candidates"] += 1          # 搜到的页整页计数，报告里看得到规模
                            if n["note_id"] in seen or n["note_id"] in known_ids:
                                rep.duplicates += 1; continue
                            if dry_run:
                                continue
                            if cap_reached():
                                done = True; continue                               # 到上限：本页剩下的只数不看，也不进 seen
                            rep.processed += 1
                            fetched_before = rep.fetched
                            try:
                                outcome = _process_note(n, name, cfg, provider, jev, triage_bank, fq_bank, rep, stats, keep_voices, min_chars)
                            except BudgetExceeded:
                                raise
                            except Exception as exc:  # noqa: BLE001 — 这条不进 seen，下次再看
                                rep.note_errors += 1; err(n["note_id"], exc, stats)
                                if isinstance(exc, DetailFailed):
                                    rep.detail_failed += 1; detail_streak += 1
                                    if detail_streak >= DETAIL_PROBE:
                                        raise DetailEndpointDown(f"连续 {DETAIL_PROBE} 篇取全文失败（TikHub 详情接口报错或返回里找不到正文），整次停；"
                                                                 f"原始返回见产物 detail_samples")
                                elif rep.fetched > fetched_before:
                                    detail_streak = 0                       # 全文取到了、是后面 Jev 没判成：详情接口是好的
                                continue
                            seen[n["note_id"]] = {"cat": name, "kw": kw, "at": rep.started, "kept": outcome == "kept", "why": outcome}
                            if outcome != "triage_reject":
                                detail_streak = 0
                            if outcome == "kept":
                                monthly[name] = monthly.get(name, 0) + 1
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
            if stats["detail_calls"] >= per_run_cap:
                stats["note"] = "本次上限已到"
            elif monthly.get(name, 0) >= per_month_cap:
                stats["note"] = "本月上限已到"
    except BudgetExceeded as exc:
        rep.stopped_reason = str(exc)
    except DetailEndpointDown as exc:
        rep.stopped_reason = str(exc)
        if rep.fetched == 0:                            # 一篇全文都没取到才算系统性故障（job 红）；中途断掉只告警，已取到的照常入库
            rep.systemic_failure = str(exc)
    except Exception as exc:  # noqa: BLE001 — 意外异常也要把已花钱拿到的产物和 state 写出去
        rep.stopped_reason = f"异常中止：{type(exc).__name__}: {exc}"
    rep.calls = provider.budget.calls; rep.spent_usd = provider.budget.spent
    rep.detail_samples = list(provider.detail_samples)
    rep.search_samples = list(getattr(provider, "search_samples", []))
    for name, s in rep.per_category.items():
        if s["candidates"]:
            continue
        if s["searched"] >= 2:
            rep.warnings.append(f"{name}：搜了 {s['searched']} 页，一条笔记都没认出来（关键词失效，或 TikHub 搜索返回格式变了；原始返回见产物 search_samples）")
        elif s["searched"] == 0 and s["errors"]:
            rep.warnings.append(f"{name}：搜索全部出错（{s['errors']} 处），这个品类本次一条没搜到")
    # 单条失败继续跑是为了不丢一整周；但「全部失败」是系统性故障（密钥失效 / 供应商或 Jev 挂了），不能悄悄绿着
    if rep.search_attempts and rep.searched == 0:
        rep.systemic_failure = f"{rep.search_attempts} 次搜索请求全部失败：TikHub 不可用、业务报错或 TIKHUB_API_KEY 失效"
    elif rep.searched and rep.candidates == 0:
        # 每页都「成功」却一条都没认出来：几十页全是真空结果不现实，多半是返回格式变了或外壳 200 里包着上游失败；白付搜索费还一直绿着
        rep.systemic_failure = rep.systemic_failure or (f"{rep.searched} 页搜索都没认出一条笔记：TikHub 搜索返回格式变了或业务报错"
                                                        f"（原始返回见产物 search_samples）")
    elif rep.processed and rep.note_errors == rep.processed:
        rep.systemic_failure = rep.systemic_failure or f"{rep.processed} 条笔记的分诊 / 取全文 / 打标全部失败：Jev 或 TikHub 详情接口不可用"
    return rep


# 发帖时间从 note_id 解（J-17，TV D-138）：小红书 note_id 是 24 位十六进制，前 8 位就是发帖时刻的 unix 秒。
# 和 TV librarian/external.py 的 posted_at、写作台发牌各自现解是同一个口径；以前 external_notes.publish_time 整列是空的（TikHub 搜索页不给）。
# 解出来的时刻早于小红书上线（2013 年）或晚于这次抓取 1 天以上，就当 id 规则变了，不写（留 NULL），不写一个错的时间进表。
_PUBLISH_FLOOR = datetime(2013, 1, 1, tzinfo=timezone.utc).timestamp()
_PUBLISH_SLACK_S = 86400


def publish_time_from_note_id(note_id: Any, fetched_at: Optional[str] = None) -> Optional[str]:
    """note_id 前 8 位十六进制 → 发帖时刻（UTC，ISO 8601 到秒，和 fetched_at 同格式）。解不出或不在合理范围 → None。"""
    head = str(note_id or "")[:8]
    if len(head) != 8 or any(c not in "0123456789abcdefABCDEF" for c in head):
        return None
    ts = int(head, 16)
    try:
        ceiling = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00")).timestamp() if fetched_at else time.time()
    except ValueError:
        ceiling = time.time()
    if ts < _PUBLISH_FLOOR or ts > ceiling + _PUBLISH_SLACK_S:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _publish_time(n: dict, fetched_at: str) -> Optional[str]:
    """平台给了就照存（不推算、不改格式）；没给（None / 空串）才从 note_id 解。"""
    given = n.get("publish_time")
    if given is not None and str(given).strip():
        return str(given)
    return publish_time_from_note_id(n.get("note_id"), fetched_at)


def external_note_rows(kept: list, run_id: str, fetched_at: Optional[str] = None) -> list:
    """给 truth_vault.external_notes（migrations/notes_v1_18）的行。fetched_at 显式带上，SQL 路径与 PostgREST 路径冲突更新时口径一样。
    publish_time：平台给了照存，没给就从 note_id 解（J-17）。"""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [{"note_id": n["note_id"], "platform": "xiaohongshu", "category": n["category"], "keyword": n["keyword"], "sort_type": n["sort_type"],
             "title": n["title"], "body": n["body"], "author": n["author"], "author_id": n["author_id"],
             "liked": n["liked"], "collected": n["collected"], "comments": n["comments"], "shares": n["shares"],
             "publish_time": _publish_time(n, fetched_at), "voice": (n.get("triage") or {}).get("voice"),
             "ad_like": (n.get("triage") or {}).get("ad_like") == "是", "has_product": (n.get("triage") or {}).get("has_product") == "是",
             "run_id": run_id, "source": "tikhub", "fetched_at": fetched_at} for n in kept]


def rows_to_sql_external(rows: list, batch: int = 100) -> str:
    if not rows:
        return "-- no rows\n"
    cols = ["note_id", "platform", "category", "keyword", "sort_type", "title", "body", "author", "author_id", "liked", "collected",
            "comments", "shares", "publish_time", "voice", "ad_like", "has_product", "run_id", "source", "fetched_at"]
    def lit(v):
        if v is None: return "NULL"
        if isinstance(v, bool): return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)): return repr(v)
        return "'" + str(v).replace("'", "''") + "'"
    out = []
    for i in range(0, len(rows), batch):
        vals = ",\n".join("(" + ", ".join(lit(r.get(c)) for c in cols) + ")" for r in rows[i:i + batch])
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "note_id")
        out.append(f"INSERT INTO truth_vault.external_notes ({', '.join(cols)}) VALUES\n{vals}\nON CONFLICT (note_id) DO UPDATE SET {upd};\n")
    return "\n".join(out)


def render_report(rep: RunReport, cfg: dict) -> str:
    L = [f"# 外部语料运行报告 · {rep.started}", "",
         f"供应商 {cfg.get('provider')} · 调用 {rep.calls} 次 · 花费 {rep.spent_usd:.2f} 美元（上限 {cfg.get('budget_usd_per_run')}）"
         + (f" · **提前停止：{rep.stopped_reason}**" if rep.stopped_reason else "")
         + (f" · **系统性故障：{rep.systemic_failure}**" if rep.systemic_failure else ""), "",
         f"搜索页 {rep.searched} · 候选 {rep.candidates} · 重复 {rep.duplicates} · 分诊通过 {rep.triaged_kept} · 取全文 {rep.fetched} · 太短丢弃 {rep.dropped_short} · 打标 {rep.judged} · 账本行 {len(rep.rows)} · 出错 {len(rep.errors)}", "",
         *([f"注意：{w}" for w in rep.warnings] + [""] if rep.warnings else []),
         "| 品类 | 搜索页 | 候选 | 分诊通过 | 取全文 | 太短 | 打标 | 出错 | 备注 |", "|---|---|---|---|---|---|---|---|---|"]
    for name, s in rep.per_category.items():
        L.append(f"| {name} | {s['searched']} | {s['candidates']} | {s.get('triaged', 0)} | {s['fetched']} | {s.get('short', 0)} | {s['judged']} | {s.get('errors', 0)} | {s.get('note', '')} |")
    if rep.errors:
        L += ["", "出错的（不进 seen，下次会再看）：", ""] + [f"- {w}: {e}" for w, e in rep.errors[:50]]
        if len(rep.errors) > 50:
            L.append(f"- …还有 {len(rep.errors) - 50} 条")
    if rep.kept_notes:
        from collections import Counter
        v = Counter((n.get("triage") or {}).get("ad_like") for n in rep.kept_notes)
        L += ["", f"留下的笔记里像广告的 {v.get('是', 0)} 篇、不像的 {v.get('否', 0)} 篇；带具体产品的 {sum(1 for n in rep.kept_notes if (n.get('triage') or {}).get('has_product') == '是')} 篇。"]
    return "\n".join(L)
