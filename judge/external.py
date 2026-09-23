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
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .banks import Bank
from .core import judge_note, judge_state, ledger_rows
from .jev_client import JevClient

TIKHUB_BASE = os.environ.get("TIKHUB_BASE_URL", "https://api.tikhub.io")
SEARCH_PATH = "/api/v1/xiaohongshu/app_v2/search_notes"
IMAGE_DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
VIDEO_DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_video_note_detail"


class BudgetExceeded(Exception):
    pass


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
            card = x.get("note_card") if isinstance(x.get("note_card"), dict) else x
            return _pick(card, "note_id", "id") is not None and _pick(card, "title", "desc", "display_title", "content") is not None
        hits = [x for x in obj if is_note(x)]
        if hits:
            return obj
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


def normalize_note(n: dict, keyword: str, category: str, sort_type: str) -> dict:
    card = n.get("note_card") if isinstance(n.get("note_card"), dict) else n
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
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})
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
        if not sess:
            found = _find_session(raw)
            if found:
                self._sessions[(keyword, sort_type)] = found
        return _find_notes(raw), raw

    def detail(self, note_id: str, note_type: str = "") -> tuple:
        path = VIDEO_DETAIL_PATH if "video" in (note_type or "").lower() or note_type == "视频笔记" else IMAGE_DETAIL_PATH
        raw = self._get(path, {"note_id": note_id})
        data = _pick(raw, "data", default=raw) or raw
        note = _pick(data, "note", "data", "note_detail", default=data) or data
        return note, raw


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
            "desc": "戒烟第15天，记录一下。昨天在药店看到咀嚼胶就买了一盒试试。嚼了几口辣嗓子，也不知道有没有用。有几次忘了带，烟瘾上来还是挺难受的。大家用过吗？"}}}


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
    """一条候选从分诊到入账本。返回结局：triage_reject / short / kept。抛 BudgetExceeded 由上层停整次运行；其它异常由上层记错继续。"""
    tr = judge_state(jev, triage_bank, n["note_id"], triage_state(n, name), subject_type="external_note")
    it = tr.items
    if it.get("on_topic", {}).get("answer") != "是" or it.get("voice", {}).get("answer") not in keep_voices:
        return "triage_reject"
    n["triage"] = {k: v["answer"] for k, v in it.items()}
    if len(n["body"]) < min_chars:
        full, _ = provider.detail(n["note_id"], n["note_type"]); rep.fetched += 1; stats["fetched"] += 1
        n["title"] = str(_pick(full, "title", default=n["title"]) or n["title"])
        n["body"] = str(_pick(full, "desc", "content", "body", default=n["body"]) or n["body"])
        n["fetched_full"] = True
    rep.triaged_kept += 1; stats["triaged"] += 1        # 取全文之后才计，预算在取全文时用完的那条不算「分诊通过」，报告四列对得上
    if len(n["body"]) < min_chars:
        rep.dropped_short += 1; stats["short"] += 1
        return "short"
    raw_text = f"标题：{n['title']}\n正文：{n['body']}" if n["title"] else n["body"]
    jr = judge_note(jev, fq_bank, n["note_id"], raw_text, title_extraction="markers", subject_type="external_note")
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

    try:
        for cat in cfg.get("categories") or []:
            name = cat["name"]
            stats = rep.per_category.setdefault(name, {"searched": 0, "candidates": 0, "triaged": 0, "kept": 0, "fetched": 0,
                                                       "short": 0, "judged": 0, "errors": 0})
            kept_this_run = 0
            if monthly.get(name, 0) >= per_month_cap:
                stats["note"] = "本月上限已到"; continue

            def cap_reached() -> bool:
                return kept_this_run >= per_run_cap or monthly.get(name, 0) >= per_month_cap

            done = False
            for kw in cat.get("keywords") or []:
                for sort_type in cfg.get("sorts") or ["general"]:
                    for page in range(1, int(cfg.get("pages_per_sort", 1)) + 1):
                        if cap_reached():
                            done = True; break
                        if page > 1 and not provider.has_session(kw, sort_type):
                            # 没有首页给的翻页会话就不花钱翻页：TikHub 会把它当新搜索返回首页等价结果
                            err(f"search {kw}/{sort_type}/p{page}", RuntimeError("首页没有返回 search_id / search_session_id，跳过翻页"), stats); break
                        try:
                            notes, _ = provider.search(kw, page, sort_type, cfg.get("note_type"))
                        except BudgetExceeded:
                            raise
                        except Exception as exc:  # noqa: BLE001 — 供应商一页失败不中止整次运行；这个关键词×排序余下的页也不翻
                            err(f"search {kw}/{sort_type}/p{page}", exc, stats); break
                        rep.searched += 1; stats["searched"] += 1
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
                            try:
                                outcome = _process_note(n, name, cfg, provider, jev, triage_bank, fq_bank, rep, stats, keep_voices, min_chars)
                            except BudgetExceeded:
                                raise
                            except Exception as exc:  # noqa: BLE001 — 这条不进 seen，下次再看
                                err(n["note_id"], exc, stats); continue
                            seen[n["note_id"]] = {"cat": name, "kw": kw, "at": rep.started, "kept": outcome == "kept", "why": outcome}
                            if outcome == "kept":
                                kept_this_run += 1; monthly[name] = monthly.get(name, 0) + 1
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
            if kept_this_run >= per_run_cap:
                stats["note"] = "本次上限已到"
            elif monthly.get(name, 0) >= per_month_cap:
                stats["note"] = "本月上限已到"
    except BudgetExceeded as exc:
        rep.stopped_reason = str(exc)
    except Exception as exc:  # noqa: BLE001 — 意外异常也要把已花钱拿到的产物和 state 写出去
        rep.stopped_reason = f"异常中止：{type(exc).__name__}: {exc}"
    rep.calls = provider.budget.calls; rep.spent_usd = provider.budget.spent
    return rep


def external_note_rows(kept: list, run_id: str, fetched_at: Optional[str] = None) -> list:
    """给 truth_vault.external_notes（migrations/notes_v1_18）的行。fetched_at 显式带上，SQL 路径与 PostgREST 路径冲突更新时口径一样。"""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [{"note_id": n["note_id"], "platform": "xiaohongshu", "category": n["category"], "keyword": n["keyword"], "sort_type": n["sort_type"],
             "title": n["title"], "body": n["body"], "author": n["author"], "author_id": n["author_id"],
             "liked": n["liked"], "collected": n["collected"], "comments": n["comments"], "shares": n["shares"],
             "publish_time": None if n.get("publish_time") is None else str(n.get("publish_time")), "voice": (n.get("triage") or {}).get("voice"),
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
         + (f" · **提前停止：{rep.stopped_reason}**" if rep.stopped_reason else ""), "",
         f"搜索页 {rep.searched} · 候选 {rep.candidates} · 重复 {rep.duplicates} · 分诊通过 {rep.triaged_kept} · 取全文 {rep.fetched} · 太短丢弃 {rep.dropped_short} · 打标 {rep.judged} · 账本行 {len(rep.rows)} · 出错 {len(rep.errors)}", "",
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
