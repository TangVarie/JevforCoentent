# -*- coding: utf-8 -*-
"""I-35 封面试点的泄露守卫：本仓、Actions 日志、step summary、产物都是公开的。

在每一个能带出东西的地方埋金丝雀（TV note_id 两种形状、项目代号、发布链接和 xsec、作品ID 格、raw_extra 里的 file token、
TikHub 外壳的 cache_url / params.note_id、昵称 / 标题 / 品牌 / fileid / trace_id、图片链接的 sign / t、live_photo、
余额绝对值和账户字段、三把密钥、Supabase 项目主机），跑一遍完整的付费路径（全是假服务），然后查 stdout / stderr、
step summary、--out 下每个文件、日志记录、警告：一只金丝雀都不许出现。"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import urllib.error
import warnings
from pathlib import Path

import pytest

from judge import external as E
from scripts import cover_pilot as P
from scripts import cover_pilot_core as K
from tests import cover_pilot_fakes as F

SUPA = "https://projref-canary.supabase.invalid"
KEY, TOKEN, SVC = "KEY-CANARY-SECRET", "TOKEN-CANARY-SECRET", "SVCKEY-CANARY-SECRET"
IMG_URL = ("https://img-canary-host.xhsfake.invalid/PATHCANARY0123456789abcdef?sign=SIGNVAL-CANARY&t=TVAL-CANARY&ap=1&sc=2"
           "&imageView2/2/w/1080/format/webp")
CACHE_URL = "https://api.tikhub.invalid/cache/CACHE-CANARY?sign=CACHESIGN-CANARY"
SAFE_STR = re.compile(r"^[A-Za-z0-9_.:\[\]/+<>=%-]{0,64}$")


def _tv_id(i: int) -> str:
    return f"TVNOTE-CANARY-{i}" if i % 2 else f"PROJCANARY_recCanary{i:04d}"     # 两种形状，后一种是 TV 真实的 {project}_rec…


def canary_tables():
    notes, source, fi, roles, xids = [], [], [], {}, []
    for i in range(12):
        nid = _tv_id(i)
        xid = hashlib.sha256(nid.encode()).hexdigest()[:24]
        xids.append(xid)
        link = f"https://xhslink.invalid/SHORT-CANARY-{i}"
        notes.append(F.note_row(nid, url=link, wu=f"{xid} ← {link}", project="PROJ-CANARY-A"))
        sha = hashlib.sha256(("sha" + nid).encode()).hexdigest()
        n_files = 1 if i < 5 else 3
        source.append({"note_id": nid, "n_files": n_files, "file_sha": sha})
        fi.append({"note_id": nid, "width": 450, "height": 600, "status": "answered", "n_files": n_files, "file_sha": sha,
                   "file_kind": "jpeg", "file_bytes": 987_654})
        roles[xid] = {"pool": "control"}
    for i in range(310):
        nid = _tv_id(100 + i)
        xid = hashlib.sha256(nid.encode()).hexdigest()[:24]
        xids.append(xid)
        if i < 300:
            link, wu = f"https://www.xiaohongshu.com/explore/{xid}?xsec_token=XSEC-CANARY", None
        elif i < 305:
            link = f"https://xhslink.invalid/SHORT-CANARY-T{i}"
            wu = f"{xid} ← {link}"
        else:
            link, wu = f"https://xhslink.invalid/SHORT-CANARY-N{i}", None
        notes.append(F.note_row(nid, url=link, wu=wu, pt=None, at=None, project="PROJ-CANARY-C"))
        roles[xid] = {"pool": "target"}
    return {"notes": notes, "v_note_first_image_source": source, "note_first_image": fi,
            "first_image_bank_state": [{"excluded_projects": ["PROJ-CANARY-X"]}], "v_notes_vanished": [], "_roles": roles}, xids


def canary_body(xid):
    items = []
    for i in range(3):
        items.append({"index": i, "fileid": f"FILEID-CANARY-{i}", "trace_id": "TRACE-CANARY", "url": IMG_URL,
                      "url_size_large": IMG_URL, "original": IMG_URL, "url_multi_level": {"high": IMG_URL},
                      "width": 900, "height": 1200, "origin_img": {"width": 900, "height": 1200, "size": 987_654},
                      "live_photo": {"url": "https://video.invalid/LIVE-CANARY"}, "text_intention": "INTENT-CANARY"})
    note = {"id": xid, "type": "normal", "model_type": "note", "title": "TITLE-CANARY", "desc": "DESC-CANARY",
            "brand": "BRAND-CANARY", "user": {"nickname": "NICK-CANARY", "user_id": "c" * 24},
            "images_list": items, "share_info": {"link": "https://www.xiaohongshu.com/discovery/item/" + xid + "?xsec_token=XSEC-CANARY"}}
    return {"code": 200, "request_id": "REQ-CANARY", "router": "/api/CANARY", "docs": "https://docs.invalid/DOCS-CANARY",
            "params": {"note_id": xid}, "cache_url": CACHE_URL, "cache_message": "CACHEMSG-CANARY",
            "data": {"code": 0, "success": True, "data": [{"note_list": [note], "model_type": "note",
                                                           "user": {"nickname": "NICK-CANARY"}}]}}


def canary_world(*, fallback=False, crash_hook=None):
    t, xids = canary_tables()
    if fallback:
        rows = []
        for r in t["notes"]:
            ex = {"笔记配图": [{"file_token": "FILETOKEN-CANARY", "name": "NAME-CANARY.jpg", "url": "https://open.feishu.invalid/x"}],
                  "_undeclared": {"作品ID": r["wu"]} if r["wu"] else {}}
            if r["pt"]:
                ex["巡查状态"] = r["pt"]
            if r["at"] is not None:
                ex["已确认存活"] = r["at"]
            rows.append({k: r[k] for k in ("note_id", "project_id", "platform", "tier", "publish_url")} | {"raw_extra": ex})

        def notes(url):
            if "raw_extra-" in P.unquote(url):
                return P.Resp(400, {}, b'{"message":"CANARY bad path"}', "")
            q = re.search(r"limit=(\d+)&offset=(\d+)", url)
            lim, off = int(q.group(1)), int(q.group(2))
            return P.Resp(200, {}, json.dumps(rows[off:off + lim], ensure_ascii=False).encode(), "")
        t["notes"] = notes
    plan = {1: "ok", 2: "ok", 3: "gone", 4: "http_error", 5: "hidden", 6: "ok", 7: "upstream", 8: "ok", 9: "url_error", 10: "ok"}

    def detail(xid, k, role):
        what = plan[k]
        if what == "http_error":
            raise urllib.error.HTTPError(f"https://api.tikhub.io/x?note_id={xid}&CANARY=1", 500, "msg CANARY " + KEY, None, None)
        if what == "url_error":
            raise urllib.error.URLError("host img-canary-host.xhsfake.invalid CANARY")
        return {"gone": F.GONE, "hidden": F.HIDDEN, "upstream": dict(F.UPSTREAM, cache_url=CACHE_URL)}.get(what) or canary_body(xid)

    def image(url, headers, k):
        if "User-Agent" not in headers:
            return P.Resp(403, {"content-type": "text/html", "set-cookie": "COOKIE-CANARY"}, b"<html>CANARY</html>", "")
        if k == 6:
            raise urllib.error.HTTPError(url, 500, "msg CANARY", None, None)
        if k == 8:
            raise urllib.error.URLError("host img-canary-host.xhsfake.invalid CANARY")
        return P.Resp(200, {"content-type": "image/webp", "x-canary": "HDR-CANARY"}, F.webp(900, 1200), "")

    fired = {"n": 0}

    def hook(world, url):
        if "img-canary-host" in url and not fired["n"]:
            fired["n"] = 1
            logging.getLogger("urllib3.connectionpool").warning("Failed to parse headers (url=%s)", IMG_URL)
            warnings.warn(ResourceWarning("unclosed <socket raddr=('203.0.113.9', 443)> CANARY " + IMG_URL))
        if crash_hook:
            crash_hook(world, url)

    bal = [P.Resp(200, {}, json.dumps({"code": 200, "user_data": {"balance": 987.654321, "email": "EMAIL-CANARY",
                                                                   "user_id": "USERID-CANARY", "account_disabled": False}}).encode(), ""),
           P.Resp(200, {}, json.dumps({"code": 200, "user_data": {"balance": 987.554321, "email": "EMAIL-CANARY"}}).encode(), "")]
    w = F.World(t, detail=detail, image=image, balances=bal, hook=hook,
                env={"SUPABASE_URL": SUPA, "SUPABASE_SERVICE_ROLE_KEY": SVC, "TIKHUB_API_KEY": KEY, "GITHUB_TOKEN": TOKEN,
                     "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "b" * 40})
    w.roles = t.get("_roles", w.roles)
    return w, xids


FORBIDDEN = ("canary", "987.6", "987.5", "987654", "://", "xsec", ".invalid", "supabase", "projref", "203.0.113")


def _all_outputs(tmp_path, capfd, caplog):
    out, err = capfd.readouterr()
    texts = {"stdout": out, "stderr": err}
    for p in sorted((tmp_path).rglob("*")):
        if p.is_file():
            texts[str(p.relative_to(tmp_path))] = p.read_text(encoding="utf-8")
    texts["caplog"] = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    return texts


def _assert_clean(texts, xids):
    for where, text in texts.items():
        low = text.lower()
        for bad in FORBIDDEN:
            assert bad not in low, (where, bad)
        for x in xids:
            assert x not in low, (where, "xhs id")
        for s in (KEY, TOKEN, SVC):
            assert s.lower() not in low, (where, "secret")


def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


@pytest.mark.parametrize("fallback", [False, True])
def test_full_paid_run_leaks_nothing(tmp_path, capfd, caplog, fallback):
    caplog.set_level(logging.DEBUG)
    w, xids = canary_world(fallback=fallback)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code = P.main(["run", "--out", str(tmp_path / "out"), "--summary", str(tmp_path / "summary.md"),
                       "--recheck-minutes", "10"], deps=w.deps())
    assert code == 0
    rep = json.loads((tmp_path / "out" / P.REPORT_JSON).read_text(encoding="utf-8"))
    # 跑到了该跑到的每一处
    assert rep["status"] == "completed" and rep["ledger"]["detail_calls"] == 10
    assert {c["status"] for c in rep["calls"]} >= {"ok", "gone", "hidden", "upstream_failed", "server", "network"}
    assert rep["selection"]["source"] == ("raw_extra_fallback" if fallback else "json_paths")
    assert rep["q3_download"]["tier_needed"] == "browser_ua" and rep["balance"]["diff_usd"] == 0.1
    assert "cache_url" in rep["q1_shape"]["envelope_keys"] and "params" in rep["q1_shape"]["envelope_keys"]
    assert any(c["recheck"] == "ok" for c in rep["calls"])
    # (a) 哪里都没有金丝雀
    texts = _all_outputs(tmp_path, capfd, caplog)
    texts["warnings"] = "\n".join(str(x.message) for x in caught)
    _assert_clean(texts, xids)
    # (b) --out 下只有两个报告文件
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [P.REPORT_JSON, P.REPORT_MD]
    # (c) 报告里每个字符串（含键）都过安全字符集；代码自己不产出要清洗的东西
    assert rep["scrubbed"] == 0
    bad = [s for s in _strings(rep) if not SAFE_STR.match(s)]
    assert not bad
    # (e) TV 只有 GET，主机就是 SUPABASE_URL 的主机
    sup = w.kinds("supabase")
    assert sup and all(m == "GET" and d is None and u.startswith(SUPA + "/rest/v1/") for _, m, d, u in sup)
    assert all("Authorization" not in x[2] and "Cookie" not in x[2] for x in w.kinds("image"))


def test_crash_prints_only_the_class_name_and_still_writes_a_report(tmp_path, capfd, monkeypatch):
    w, xids = canary_world()
    real = P._pace_detail
    n = {"k": 0}

    def boom(st, deps):
        n["k"] += 1
        if n["k"] == 3:
            raise RuntimeError("CANARY in message " + IMG_URL)
        return real(st, deps)
    monkeypatch.setattr(P, "_pace_detail", boom)
    code = P.main(["run", "--out", str(tmp_path / "out"), "--summary", str(tmp_path / "summary.md")], deps=w.deps())
    out, err = capfd.readouterr()
    assert code == 1 and "RuntimeError" in err
    rep = json.loads((tmp_path / "out" / P.REPORT_JSON).read_text(encoding="utf-8"))
    assert rep["status"] == "crashed" and rep["exception_class"] == "RuntimeError" and rep["scrubbed"] == 0
    assert len(w.kinds("detail")) == 2 and len(w.kinds("balance")) == 2          # 出错了也在 finally 里读了余额
    _assert_clean({"stdout": out, "stderr": err, "md": (tmp_path / "out" / P.REPORT_MD).read_text(encoding="utf-8"),
                   "json": json.dumps(rep), "summary": (tmp_path / "summary.md").read_text(encoding="utf-8")}, xids)


def test_sigterm_handler_raises_system_exit():
    with pytest.raises(SystemExit):
        P._on_term(15, None)


def test_say_redacts_lines_that_look_like_ids_urls_or_secrets(capsys):
    P._SECRETS[:] = [KEY]
    try:
        for line in ("https://x.invalid/a", "id 0123456789abcdef0123", "n 1234567890", "xsec_token=1", "a@b",
                     f"key {KEY}"):
            P.say(line)
        P.say("call 3/10 T1 target long_explore: gone")
    finally:
        P._SECRETS.clear()
    lines = capsys.readouterr().out.splitlines()
    assert lines[:6] == ["[redacted line]"] * 6 and lines[6] == "call 3/10 T1 target long_explore: gone"


def test_scrub_replaces_anything_off_vocabulary():
    rep = {"ok_key": ["ok", "hex_1_8", "2026-11-18", "http_404", "KeyError"], "PROJCANARY_recCanary0001": 1,
           "x": ["https://a.invalid/x", "nrt_2_recabcdefgh", "PROJ-CANARY", "0123456789abcdef0123", "标题"]}
    clean, n = K.scrub(rep)
    assert clean["ok_key"] == ["ok", "hex_1_8", "2026-11-18", "http_404", "KeyError"]
    assert clean["x"] == ["redacted"] * 5 and n == 6 and "PROJCANARY_recCanary0001" not in clean
    assert K.scrub({"v": float("nan")})[0] == {"v": None}


def test_hardening_is_applied_inside_main_and_restored(tmp_path):
    names = ("urllib3", "urllib3.connectionpool", "requests")
    before = [(logging.getLogger(n).level, logging.getLogger(n).propagate, len(logging.getLogger(n).handlers)) for n in names]
    restore = P._harden()
    try:
        for n in names:
            lg = logging.getLogger(n)
            assert any(isinstance(h, logging.NullHandler) for h in lg.handlers)
            assert lg.level >= logging.ERROR and lg.propagate is False
        assert logging.raiseExceptions is False
    finally:
        restore()
    assert [(logging.getLogger(n).level, logging.getLogger(n).propagate, len(logging.getLogger(n).handlers)) for n in names] == before
    src = (P.ROOT / "scripts" / "cover_pilot.py").read_text(encoding="utf-8")
    top = [ast.unparse(n.value.func) for n in ast.parse(src).body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert top == ["sys.path.insert"]                                # import 时除了本仓脚本惯例的 sys.path 不调用任何东西
    assert "basicConfig" not in src


def test_only_post_is_the_claim_and_no_other_write_verbs():
    src = (P.ROOT / "scripts" / "cover_pilot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    posts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "POST"]
    assert len(posts) == 1
    claim = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "claim_run")
    assert any(isinstance(n, ast.Constant) and n.value == "POST" for n in ast.walk(claim))
    assert P.GITHUB_REFS.startswith("https://api.github.com/repos/")
    for verb in ("PATCH", "PUT", "DELETE"):
        assert f'"{verb}"' not in src and f"'{verb}'" not in src
    core = (P.ROOT / "scripts" / "cover_pilot_core.py").read_text(encoding="utf-8")
    for s in (src, core):
        assert "urlopen" not in s and "requests." not in s


def test_pinned_constants_and_no_base_url_override():
    for name in ("cover_pilot.py", "cover_pilot_core.py", "cover_pilot_rehearse.py"):
        assert "TIKHUB_BASE_URL" not in (P.ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert P.TIKHUB_BASE == "https://api.tikhub.io" and P.DETAIL_PATH is E.DETAIL_PATH
    assert P.BALANCE_PATH == "/api/v1/tikhub/user/get_user_info"


def test_reports_never_contain_absolute_balance_fields():
    w, _ = canary_world()
    word, bal = P.tikhub_balance(w.fetch, KEY)
    assert word == "ok" and bal == 987.654321                       # 只回一个浮点，账户的其他字段当场丢
    st = {"balance_before": 987.654321, "balance_after": 987.554321, "ledger": K.Ledger(), "floor": "ok"}
    sec = P._balance_section(st)
    assert set(sec) == {"diff_usd", "diff_yuan", "expected_usd", "excess_usd", "label", "floor", "after_read"}
    assert "987" not in json.dumps(sec)
