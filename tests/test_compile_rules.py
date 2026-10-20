# -*- coding: utf-8 -*-
"""2026-10-20：项目层全部放行（"*"）、Jev 格式题的 scope（标题也能当证据）、POST /compile_rules（自由文本规则 → 写后检查）。
全部 mock，不联网；规则与稿子都是合成的，不含任何客户原文。"""
from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge import banks as B  # noqa: E402
from judge import policy as P  # noqa: E402
from judge import rules as R  # noqa: E402

PH = ROOT / "banks" / "platform_health_v0.1.yaml"
TITLE = "通勤一小时我做了这些"
BODY = "上周开始坐地铁上班，单程一个小时。\n路上看完了两本书，挺充实的。\n你们路上都干嘛？"
SYNTH_RULES = [   # 合成规则：形状覆盖 code / questions / not_checkable / 两样都有
    {"id": "r-001", "text": "标题里不要出现「最便宜」「第一」", "severity": "hard"},
    {"id": "r-002", "text": "正文控制在300字以内", "severity": "soft"},
    {"id": "r-003", "text": "每批稿子按 日期_序号 命名后交付", "severity": "soft"},
    {"id": "r-004", "text": "人设是刚工作两年的上班族，前后不要矛盾", "severity": "soft"},
    {"id": "r-005", "text": "标题要让人一看就知道是讲通勤的", "severity": "soft"},
    {"id": "r-006", "text": "不要用「闭眼入」，整体口吻别像广告", "severity": "soft"},
]


def _client(monkeypatch, **env):
    monkeypatch.setenv("JUDGE_MOCK", "1"); monkeypatch.setenv("JUDGE_API_KEY", "k-admin")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from judge import api as A
    return TestClient(A.app), {"X-Judge-Key": "k-admin"}


# ── 1. 数据出境：project_layer_cleared 的 "*" ──

def test_project_layer_wildcard_explicit_list_and_rx_still_first():
    every = {"rx_categories": ["处方药"], "rx_projects": ["OKMAN"], "rx_cleared": [], "project_layer_cleared": ["*"]}
    assert P.decide(["aw_version"], "ANY_NEW_PROJECT", cfg=every).project_layer is True
    assert P.decide([], "XIWU_phase2", cfg=every, published=False).project_layer is True
    listed = dict(every, project_layer_cleared=["TUGE"])                         # 显式名单照旧
    assert P.decide(["aw_version"], "TUGE", cfg=listed).project_layer is True
    assert P.decide(["aw_version"], "NRT", cfg=listed).project_layer is False
    assert P.decide(["aw_version"], "NRT", cfg=dict(every, project_layer_cleared=[])).project_layer is False
    # 处方药闸先执行，"*" 放不过：没清出境的处方药项目 / 处方药品类照样 PolicyBlocked
    with pytest.raises(P.PolicyBlocked):
        P.decide(["aw_version"], "OKMAN", cfg=every)
    with pytest.raises(P.PolicyBlocked):
        P.decide(["aw_version"], "ANY", "处方药", cfg=every)
    with pytest.raises(P.PolicyBlocked):                                         # 通配只在 project_layer_cleared 认
        P.decide(["aw_version"], "OKMAN", cfg=dict(every, rx_cleared=["*"]))
    cleared_rx = dict(every, rx_cleared=["OKMAN"])
    assert P.decide(["aw_version"], "OKMAN", cfg=cleared_rx).project_layer is True
    assert P.decide(["note"], "OKMAN", cfg=every).published is True              # 公开内容不受影响


def test_repo_policy_config_pins_owner_decision_2026_10_20(monkeypatch):
    monkeypatch.setattr(P, "CONFIG", ROOT / "config" / "data_policy.yaml")
    cfg = P.load()
    assert cfg["project_layer_cleared"] == ["*"]                                   # 「全部放行，一次到位」；YAML 里必须带引号
    assert {"OKMAN", "OKMAN_phase1"} <= set(cfg["rx_cleared"]) and "*" not in cfg["rx_cleared"]
    assert P.decide(["aw_version"], "XIWU_phase2").project_layer is True
    assert P.decide(["aw_version"], "OKMAN_phase1").project_layer is True
    with pytest.raises(P.PolicyBlocked):
        P.decide(["aw_version"], "SOME_RX", "处方药")


def test_repo_config_runs_project_layer_over_http(monkeypatch):
    c, H = _client(monkeypatch)
    monkeypatch.setattr(P, "CONFIG", ROOT / "config" / "data_policy.yaml")
    brief = {"hard_rules": [{"id": "must_ask", "ask": "有没有向读者提问？", "want": True}]}
    d = c.post("/judge_draft", json={"title": TITLE, "body": BODY, "project": "BRAND_NEW", "brief": brief}, headers=H).json()
    assert d["policy"]["project_layer"] and d["policy"]["dropped_banks"] == [] and "must_ask" in d["detail"]["project"]
    assert c.post("/judge_draft", json={"title": TITLE, "body": BODY, "project": "X", "category": "处方药"}, headers=H).status_code == 403


# ── 3. Jev 格式题的 scope：标题自成一句进证据 ──

class RecordingJev:
    """非证据题一律答「是」（0.9）；证据题选第一个文字里含 pick 的编号句，没有就选「没有」。记下每次请求。"""

    def __init__(self, pick: str = ""):
        self.pick, self.bodies, self.mock = pick, [], False

    def call(self, body):
        self.bodies.append(body)
        qs = body["questions"]
        if all(set(q["criteria"]) - {"没有"} <= {str(i) for i in range(1, 300)} for q in qs.values()):
            ans = {}
            for qid, q in qs.items():
                hit = next((k for k, v in q["criteria"].items() if k != "没有" and self.pick and self.pick in v), "没有")
                ans[qid] = {"type": "choice", "choice": hit, "probabilities": {hit: 0.9}}
            return {"answers": ans, "usage": {"input_tokens": 1}}
        return {"answers": {q: {"type": "noul", "noul": 0.9} for q in qs}, "usage": {"input_tokens": 1}}


def _scoped_bank(**scopes) -> dict:
    qs = []
    for qid, scope in scopes.items():
        q = {"id": qid, "type": "noul", "instructions": {"zh": f"{qid}：有没有讲通勤？"},
             "criteria": {"true": {"zh": "是"}, "false": {"zh": "否"}}, "evidence": "on_true"}
        if scope:
            q["scope"] = scope
        qs.append(q)
    return {"bank_version": "t-1", "questions": qs}


def test_jev_scope_loader_prefix_and_existing_banks_unchanged():
    b = B.load_bank_data(_scoped_bank(t_q="title", f_q="full", b_q="body", n_q=None), "proj")
    by = b.by_id()
    assert (by["t_q"].scope, by["f_q"].scope, by["b_q"].scope, by["n_q"].scope) == ("title", "full", "body", None)
    assert by["t_q"].instructions.startswith("只看【标题】") and by["t_q"].ask == "t_q：有没有讲通勤？"
    assert by["f_q"].instructions.startswith("看【标题】和【正文】") and by["n_q"].instructions == by["n_q"].ask
    assert B.check_bank(b) == []
    ph = B.load_bank(PH)                                                            # 现有题库：没 scope，题干一字不变
    assert all(q.scope is None and q.instructions == q.ask for q in ph.questions)
    bad = B.load_bank_data(_scoped_bank(x_q="first_sentence"), "proj")
    assert any("scope" in p for p in B.check_bank(bad))


def test_title_scope_finds_evidence_in_title_where_body_only_could_not():
    """critic 18：标题题答「是」，以前证据只在正文句里找 → evidence_not_found → 判不出。"""
    from judge import loop as L
    bank = B.load_bank_data(_scoped_bank(t_q="title", f_q="full", n_q=None), "proj")
    jev = RecordingJev(pick=TITLE)
    dj = L.judge_draft(jev, {"title": TITLE, "body": BODY}, project=bank, hard_rules={("proj", "n_q"): "是", ("proj", "t_q"): "是"})
    items = dj.detail["proj"]
    assert items["t_q"]["answer"] == "是" and items["t_q"]["evidence"] == TITLE and not items["t_q"].get("invalid_reason")
    assert items["f_q"]["evidence"] == TITLE
    assert items["n_q"]["invalid_reason"] == "evidence_not_found"                                       # 没 scope：照旧只在正文找
    from judge import core as C
    rows = {r["question_id"]: r for r in C.ledger_rows(dj.results["proj"], bank)}
    assert rows["n_q"]["answer"] is None and rows["t_q"]["answer"] == "是" and rows["t_q"]["evidence"] == TITLE
    ev = next(b for b in jev.bodies if "说明" in b["state"] and "编号" in b["state"]["说明"])["questions"]
    assert [v for k, v in ev["t_q"]["criteria"].items() if k != "没有"] == [TITLE]                    # 标题自成一句
    assert list(ev["f_q"]["criteria"].values())[0] == TITLE and len(ev["f_q"]["criteria"]) == 1 + 1 + 3  # 标题 + 正文三句 + 没有
    assert TITLE not in json.dumps(ev["n_q"]["criteria"], ensure_ascii=False)


def test_title_scope_without_title_is_no_title_not_unjudged():
    from judge import core as C, loop as L
    bank = B.load_bank_data(_scoped_bank(t_q="title", n_q=None), "proj")
    jev = RecordingJev(pick="单程")
    dj = L.judge_draft(jev, {"title": "", "body": BODY}, project=bank, hard_rules={("proj", "t_q"): "是"})
    assert dj.detail["proj"]["t_q"]["invalid_reason"] == "no_title" and dj.unjudged == [] and not dj.hard_fails
    assert all("t_q" not in b["questions"] for b in jev.bodies)                                           # 不问
    rows = C.ledger_rows(dj.results["proj"], bank)
    assert {r["question_id"]: r["invalid_reason"] for r in rows}["t_q"] == "no_title"
    only_title = B.load_bank_data(_scoped_bank(t_q="title"), "proj")                                     # 整份都是标题题：零调用
    jev2 = RecordingJev()
    dj2 = L.judge_draft(jev2, {"title": "  ", "body": BODY}, project=only_title)
    assert jev2.bodies == [] and dj2.calls == 0 and dj2.detail["proj"]["t_q"]["invalid_reason"] == "no_title"
    assert [r["invalid_reason"] for r in C.ledger_rows(dj2.results["proj"], only_title)] == ["no_title"]


def test_inline_project_bank_scope_over_http(monkeypatch):
    c, H = _client(monkeypatch)
    from judge import api as A
    monkeypatch.setattr(A, "_client", lambda: RecordingJev(pick=TITLE))
    d = c.post("/judge_draft", json={"title": TITLE, "body": BODY, "project": "ANY", "banks": ["platform_health_v0.1"],
                                     "project_bank": _scoped_bank(t_q="title"), "judge_paras": "never"}, headers=H)
    assert d.status_code == 200, d.text
    assert d.json()["detail"]["project"]["t_q"]["evidence"] == TITLE
    bad = c.post("/judge_draft", json={"title": TITLE, "body": BODY, "project": "ANY", "project_bank": _scoped_bank(x_q="last_para")}, headers=H)
    assert bad.status_code == 422 and "scope" in bad.json()["detail"]


# ── 2 / 4. POST /compile_rules ──

def test_compile_rules_auth_writer_403_and_unknown_401(monkeypatch):
    c, H = _client(monkeypatch, JUDGE_WRITER_API_KEYS="w1")
    called = []
    monkeypatch.setattr(R, "make_generator", lambda: called.append(1) or R.MockRuleGenerator())
    body = {"project": "ANY", "rules": SYNTH_RULES[:1]}
    r = c.post("/compile_rules", json=body, headers={"X-Judge-Key": "w1"})
    assert r.status_code == 403 and "compile_rules" in r.json()["detail"]
    assert c.post("/compile_rules", json=body, headers={"X-Judge-Key": "nope"}).status_code == 401
    assert c.post("/compile_rules", json=body).status_code == 401
    assert called == []
    assert c.post("/compile_rules", json=body, headers=H).status_code == 200 and called == [1]


def test_compile_rules_policy_cleared_vs_not(monkeypatch, policy_cfg):
    c, H = _client(monkeypatch)
    called = []
    monkeypatch.setattr(R, "make_generator", lambda: called.append(1) or R.MockRuleGenerator())
    body = {"rules": SYNTH_RULES[:2]}
    r = c.post("/compile_rules", json=dict(body, project="NRT"), headers=H)                # policy_cfg：NRT 项目层没放行
    assert r.status_code == 403 and r.json()["detail"].startswith("policy:")
    assert c.post("/compile_rules", json=dict(body, project="OKMAN"), headers=H).status_code == 403          # 处方药没清出境
    assert c.post("/compile_rules", json=dict(body, project="TUGE", category="处方药"), headers=H).status_code == 403
    assert c.post("/compile_rules", json=dict(body, project="  "), headers=H).status_code == 422
    assert called == []                                                                     # 被拦的一次生成端都不建
    ok = c.post("/compile_rules", json=dict(body, project="TUGE"), headers=H)
    assert ok.status_code == 200 and ok.json()["policy"]["project_layer"] is True and called == [1]
    policy_cfg.write_text('rx_categories: [处方药]\nrx_projects: [OKMAN]\nrx_cleared: []\nproject_layer_cleared: ["*"]\n', encoding="utf-8")
    assert c.post("/compile_rules", json=dict(body, project="NRT"), headers=H).status_code == 200            # 通配：都放行
    assert c.post("/compile_rules", json=dict(body, project="OKMAN"), headers=H).status_code == 403          # 处方药仍先拦


def test_compile_rules_request_validation(monkeypatch):
    c, H = _client(monkeypatch)
    one = SYNTH_RULES[0]
    bad_bodies = [
        {"rules": [one]},                                                                   # 没 project
        {"project": "ANY", "rules": []},
        {"project": "ANY", "rules": [dict(one, id=f"r{i}") for i in range(61)]},
        {"project": "ANY", "rules": [dict(one, text="字" * 4001)]},
        {"project": "ANY", "rules": [dict(one, severity="medium")]},
        {"project": "ANY", "rules": [{"id": "r", "text": "x"}]},                            # severity 必填
        {"project": "ANY", "rules": [dict(one, scope="标题")]},                             # 多余字段
        {"project": "ANY", "rules": [dict(one, applicability="字" * 101)]},
        {"project": "ANY", "rules": [one, dict(one)]},                                      # id 重复
        {"project": "ANY", "rules": [dict(one, text="   ")]},
    ]
    for b in bad_bodies:
        assert c.post("/compile_rules", json=b, headers=H).status_code == 422, b
    assert c.post("/compile_rules", json={"project": "ANY", "rules": [dict(one, id=f"r{i}") for i in range(60)]}, headers=H).status_code == 200


def test_compile_rules_mock_verdict_shapes_and_qids(monkeypatch):
    c, H = _client(monkeypatch)
    d = c.post("/compile_rules", json={"project": "ANY", "category": "教育", "rules": SYNTH_RULES}, headers=H).json()
    by = {r["id"]: r for r in d["rules"]}
    assert [r["id"] for r in d["rules"]] == [r["id"] for r in SYNTH_RULES] and d["errors"] == 0 and d["model"] == "mock"
    assert by["r-001"]["verdict"] == "code" and by["r-001"]["code"] == {"kind": "forbidden_word", "words": ["最便宜", "第一"], "where": ["title"]}
    assert by["r-001"]["questions"] == []
    assert by["r-002"]["code"] == {"kind": "max_len", "max": 300, "where": "body"}
    assert by["r-003"] == {"id": "r-003", "sha": R.rule_sha(SYNTH_RULES[2]["text"]), "verdict": "not_checkable", "code": None,
                           "questions": [], "coverage": None, "uncovered": "", "why": by["r-003"]["why"]}
    assert all(r["coverage"] == "full" for r in d["rules"] if r["verdict"] in ("code", "questions"))
    assert by["r-006"]["verdict"] == "questions" and by["r-006"]["code"]["kind"] == "forbidden_word" and len(by["r-006"]["questions"]) == 1
    assert by["r-005"]["questions"][0]["scope"] == "title"
    for r, src in zip(d["rules"], SYNTH_RULES):
        assert r["sha"] == __import__("hashlib").sha256(src["text"].encode("utf-8")).hexdigest()[:12]
        for n, q in enumerate(r["questions"], 1):
            assert R.QID_RE.match(q["qid"]) and q["qid"] == f"{re.sub('[^0-9a-z]', '', r['id'])}_{r['sha'][:6]}_{n}"
            assert set(q) == {"qid", "ask", "pass_if", "scope", "note"} and q["pass_if"] in ("是", "否")
    qids = [q["qid"] for r in d["rules"] for q in r["questions"]]
    assert len(qids) == len(set(qids)) == len(d["project_bank"]["questions"])
    assert all(not (set(qids) & set(B.load_bank(p).ids())) for p in (ROOT / "banks").glob("*.yaml"))
    assert d["calls"] == 1 and d["policy"]["project_layer"]


class ScriptedGen:
    """按顺序吐预设的输出；callable 项按 prompt 现算。记下每次的 prompt。"""

    model = "scripted"

    def __init__(self, outputs):
        self.outputs, self.prompts, self.calls, self.usage = list(outputs), [], 0, {}

    def __call__(self, prompt, n=1):
        self.prompts.append(prompt); self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return [out(prompt) if callable(out) else out] * n


def _ok(rid, verdict="not_checkable", **kw):
    base = {"id": rid, "verdict": verdict, "code": None, "questions": [], "why": "合成",
            "coverage": None if verdict == "not_checkable" else "full", "uncovered": ""}
    base.update(kw)
    return base


def test_bad_json_retries_once_then_per_rule_error(monkeypatch):
    c, H = _client(monkeypatch)
    a, b = {"id": "a", "text": "规则甲", "severity": "soft"}, {"id": "b", "text": "规则乙", "severity": "soft"}
    good_q = {"ask": "正文有没有提到价格？", "pass_if": "否", "scope": "body", "note": "合成"}
    gen = ScriptedGen(["这不是 JSON", json.dumps({"results": [_ok("a", "questions", questions=[good_q]),
                                                                 _ok("b", "questions", questions=[dict(good_q, pass_if="也许")])]},
                                                   ensure_ascii=False)])
    monkeypatch.setattr(R, "make_generator", lambda: gen)
    d = c.post("/compile_rules", json={"project": "ANY", "rules": [a, b]}, headers=H).json()
    by = {r["id"]: r for r in d["rules"]}
    assert gen.calls == 2 and d["calls"] == 2 and d["errors"] == 1                         # 只重试一次
    assert "error" not in by["a"] and by["a"]["questions"][0]["qid"] == f"a_{R.rule_sha('规则甲')[:6]}_1"
    assert by["b"]["verdict"] is None and "已重试一次" in by["b"]["error"] and "pass_if" in by["b"]["error"]
    assert "上一次的输出有问题" in gen.prompts[1] and "不是合法 JSON" in gen.prompts[1]
    assert [q["id"] for q in d["project_bank"]["questions"]] == [by["a"]["questions"][0]["qid"]]
    # 第一次只有一条不合格：重试只带那一条
    gen2 = ScriptedGen([json.dumps({"results": [_ok("a")]}), lambda p: json.dumps({"results": [_ok(r["id"]) for r in R.rules_in_prompt(p)]})])
    monkeypatch.setattr(R, "make_generator", lambda: gen2)
    d2 = c.post("/compile_rules", json={"project": "ANY", "rules": [a, b]}, headers=H).json()
    assert d2["errors"] == 0 and [r["id"] for r in R.rules_in_prompt(gen2.prompts[1])] == ["b"] and "输出里没有这条规则" in gen2.prompts[1]
    assert d2["project_bank"] is None                                                       # 一道题都没有
    # 全部编不出：502（同 /judge 全部失败）；生成端调不通也是
    monkeypatch.setattr(R, "make_generator", lambda: ScriptedGen(["{", "["]))
    r3 = c.post("/compile_rules", json={"project": "ANY", "rules": [a]}, headers=H)
    assert r3.status_code == 502 and "一条都没编出来" in r3.json()["detail"]
    monkeypatch.setattr(R, "make_generator", lambda: ScriptedGen([TimeoutError("timed out")]))
    r4 = c.post("/compile_rules", json={"project": "ANY", "rules": [a]}, headers=H)
    assert r4.status_code == 502 and "生成端调用失败" in r4.json()["detail"]


def test_validate_result_and_code_shapes():
    V = R.validate_result
    q = {"ask": "标题里有没有提到季节？", "pass_if": "是", "scope": "title"}
    assert V(_ok("x", "code", code={"kind": "max_len", "min": 300}))["code"] == {"kind": "max_len", "min": 300, "where": "body"}
    fw = V(_ok("x", "code", code={"kind": "forbidden_word", "words": ["甲乙", "甲乙 ", "丙丁", "a"]}))["code"]
    assert fw["where"] == ["title", "body"] and fw["words"] == ["甲乙", "丙丁", "a"]          # 去重；纯英文单字母写作台按词边界认，放行
    assert V(_ok("x", "questions", questions=[q], code={"kind": "required_phrase", "any": ["丙"], "where": "tags"}))["code"]["where"] == ["tags"]
    assert V(_ok("x", "questions", questions=[q]))["questions"] == [dict(q, note="")]
    tags = V(_ok("x", "code", code={"kind": "forbidden_regex", "pattern": r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S", "where": ["tags"]}))["code"]  # prompt 里教的标签计数写法
    assert re.search(tags["pattern"], "#a #b #c #d #e #f") and not re.search(tags["pattern"], "#a #b #c #d #e")
    bad = [
        _ok("x", "maybe"),
        _ok("x", "code"),                                                                      # 没 code
        _ok("x", "code", code={"kind": "max_len", "max": 5}, questions=[q]),                   # code 却带题
        _ok("x", "not_checkable", code={"kind": "max_len", "max": 5}),
        _ok("x", "questions"),
        _ok("x", "questions", questions=[q] * 4),
        _ok("x", "questions", questions=[dict(q, scope="first_sentence")]),
        _ok("x", "questions", questions=[dict(q, ask="")]),
        _ok("x", "code", code={"kind": "max_len", "max": 5, "min": 9}),
        _ok("x", "code", code={"kind": "max_len", "max": True}),
        _ok("x", "code", code={"kind": "max_len", "max": 5, "where": "tags"}),
        _ok("x", "code", code={"kind": "forbidden_word", "words": ["甲"], "where": ["封面"]}),
        _ok("x", "code", code={"kind": "forbidden_word", "words": ["甲"], "target": "乙"}),    # 不认识的键
        _ok("x", "code", code={"kind": "forbidden_word", "words": ["字" * 41]}),
        _ok("x", "code", code={"kind": "forbidden_regex", "pattern": "(a+)+$"}),               # 灾难性回溯
        _ok("x", "code", code={"kind": "forbidden_regex", "pattern": "(未闭合"}),
        _ok("x", "code", code={"kind": "emoji_count", "max": 3}),
    ]
    for raw in bad:
        with pytest.raises(R.RuleOutputError):
            V(raw)
    assert R.parse_output('```json\n{"results": []}\n```') == []
    with pytest.raises(R.RuleOutputError):
        R.parse_output('{"rules": []}')


def test_prompt_examples_pass_our_own_validator():
    """prompt 里教给模型的每个 code 形状和整段示意都要过本仓的校验（不然模型照抄示意也会被打回）。"""
    lines = [ln.strip().split("   ")[0] for ln in R.PROMPT_HEAD.splitlines() if ln.strip().startswith('{"kind"')]
    assert len(lines) == 7
    codes = [R.validate_code(json.loads(ln)) for ln in lines]
    example = R.PROMPT_HEAD.split("示意（规则是虚构的，只看格式）：\n", 1)[1]
    got = [R.validate_result(x) for x in R.parse_output(example)]
    assert [g["verdict"] for g in got] == ["code", "questions", "not_checkable", "code"]
    assert [g["coverage"] for g in got] == ["full", "full", None, "partial"] and got[3]["uncovered"]
    # 教的写法在写作台的执行方式下（re.search、无 flag）真的对：极限词带上下文不撞普通词；计数跨换行
    extreme = next(c["pattern"] for c in codes if "第一" in c.get("pattern", ""))
    assert not re.search(extreme, "最近通勤的小习惯\n第一次试着在地铁上看书") and re.search(extreme, "这是最便宜的一款")
    count3 = next(c["pattern"] for c in codes if c.get("pattern", "").startswith("品牌"))
    assert re.search(count3, "品牌一\n品牌二\n品牌三") and not re.search(count3, "品牌一\n品牌二")


def test_qids_stable_change_with_text_and_never_clash():
    gen = R.MockRuleGenerator()
    rule = {"id": "3F2A9C1E-77aa-4d0e", "text": "人设前后不要矛盾", "severity": "soft"}
    one = R.compile_rules(gen, [rule])[0]
    assert one["questions"][0]["qid"] == f"3f2a9c1e_{R.rule_sha(rule['text'])[:6]}_1"
    assert R.compile_rules(gen, [rule])[0]["questions"] == one["questions"]                      # 同规则同原文：题号稳定
    edited = R.compile_rules(gen, [dict(rule, text=rule["text"] + "。")])[0]
    assert edited["sha"] != one["sha"] and edited["questions"][0]["qid"] != one["questions"][0]["qid"]   # 改了原文：新题号
    same_text = R.compile_rules(gen, [rule, dict(rule, id="global-12")])
    assert len({r["questions"][0]["qid"] for r in same_text}) == 2                                 # 原文相同、id 不同：不撞
    clash = R.compile_rules(gen, [dict(rule, id="r-1"), dict(rule, id="r_1")])                     # 缩写相同且原文相同：后一条带 error
    assert "error" not in clash[0] and "撞车" in clash[1]["error"]
    taken = R.compile_rules(gen, [rule], taken={one["questions"][0]["qid"]})[0]
    assert "撞车" in taken["error"]
    assert R.rule_short("规则一") == "r"
    with pytest.raises(ValueError):
        R.to_project_bank([one, dict(one, id="dup")])
    with pytest.raises(ValueError):
        R.to_project_bank([one], taken={one["questions"][0]["qid"]})


def test_to_project_bank_loads_as_jev_bank_without_hard_rules():
    gen = R.MockRuleGenerator()
    res = R.compile_rules(gen, [dict(r) for r in SYNTH_RULES])
    pb = R.to_project_bank(res)
    bank = B.load_bank_data(pb, "desk_rules")
    assert B.check_bank(bank) == [] and bank.fmt == "jev"
    assert bank.ids() == [q["qid"] for r in res for q in r["questions"]]
    assert {q.id: q.scope for q in bank.questions} == {q["qid"]: q["scope"] for r in res for q in r["questions"]}
    pass_if = {q["qid"]: q["pass_if"] for r in res for q in r["questions"]}
    assert all(q.jtype == "noul" and q.evidence == ("on_true" if pass_if[q.id] == "否" else "never") for q in bank.questions)
    assert set(pass_if.values()) == {"是", "否"}                                                 # 两种都覆盖到
    assert pb["bank_version"].startswith("rules-") and "hard_rules" not in pb
    assert R.to_project_bank([dict(r, questions=r["questions"]) for r in res if r["verdict"] != "questions"]) is None


def test_compile_then_judge_draft_round_trip_is_advice_only(monkeypatch):
    c, H = _client(monkeypatch)
    d = c.post("/compile_rules", json={"project": "ANY", "rules": SYNTH_RULES}, headers=H).json()
    qids = [q["qid"] for r in d["rules"] for q in r["questions"]]
    draft = {"title": TITLE, "body": BODY, "project": "ANY", "subject_id": "v1"}
    with_rules = c.post("/judge_draft", json=dict(draft, project_bank=d["project_bank"], project_bank_name="desk_rules"), headers=H)
    assert with_rules.status_code == 200, with_rules.text
    w = with_rules.json()
    assert set(w["detail"]["desk_rules"]) == set(qids) and w["policy"]["dropped_banks"] == []
    assert not any(k.startswith("desk_rules:") for k in w["hard_rules"])                      # 只作建议：不进硬约束
    assert not any(f[0] == "desk_rules" for f in w["hard_fails"]) and not any(u[0] == "desk_rules" for u in w["unjudged"])
    plain = c.post("/judge_draft", json=draft, headers=H).json()
    assert w["passed"] == plain["passed"]                                                     # 加不加规则题，过不过都一样
    assert {r["question_id"] for r in w["ledger_rows"]} >= set(qids)
    # README 建议的影子请求：单独一次、banks 为空、只带规则题、自己的 run_tag、不判段
    lean = c.post("/judge_draft", json=dict(draft, banks=[], project_bank=d["project_bank"], project_bank_name="desk_rules",
                                            run_tag="shadow-rules", judge_paras="never"), headers=H)
    assert lean.status_code == 200, lean.text
    lj = lean.json()
    assert set(lj["detail"]) == {"desk_rules"} and set(lj["detail"]["desk_rules"]) == set(qids)
    assert {r["run_tag"] for r in lj["ledger_rows"]} == {"shadow-rules"}


def test_compile_batches_run_in_parallel_and_cover_every_rule(monkeypatch):
    barrier = threading.Barrier(3, timeout=5)

    def gen(prompt, n=1):
        barrier.wait()                                                                          # 串行就会在这里超时 → 整批报错
        return [json.dumps({"results": [_ok(r["id"]) for r in R.rules_in_prompt(prompt)]})]

    rules = [{"id": f"r{i}", "text": f"合成规则{i}", "severity": "soft"} for i in range(7)]
    out = R.compile_rules(gen, rules, batch_size=3, workers=3)
    assert [r["id"] for r in out] == [r["id"] for r in rules] and not any(r.get("error") for r in out)


def test_make_generator_env_and_request_body(monkeypatch):
    monkeypatch.delenv("JUDGE_MOCK", raising=False)
    for k in ("ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "JUDGE_COMPILE_MODEL", "JUDGE_COMPILE_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(R.GeneratorNotConfigured):
        R.make_generator()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    g = R.make_generator()
    assert g.model == "claude-sonnet-5-5" and g.temperature is None and g.retries == 1 and g.timeout == 90.0
    sent = []
    monkeypatch.setattr(g, "_post", lambda body: sent.append(body) or {"content": [{"type": "text", "text": "{}"}],
                                                                         "usage": {"input_tokens": 7, "output_tokens": 3}})
    g("p"); g("p")
    assert "temperature" not in sent[0] and sent[0]["output_config"] == {"effort": "low"} and g.usage == {"input_tokens": 14, "output_tokens": 6}
    monkeypatch.setenv("JUDGE_COMPILE_EFFORT", "")                                              # 中转站不认 output_config 时
    monkeypatch.setenv("JUDGE_COMPILE_MODEL", "kimi-k2")
    g2 = R.make_generator()
    assert g2.extra == {} and g2.model == "kimi-k2"
    # HTTP：没配密钥 → 503，不是 500
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("JUDGE_API_KEY", "k")
    from fastapi.testclient import TestClient
    from judge import api as A
    tc = TestClient(A.app)
    r = tc.post("/compile_rules", json={"project": "ANY", "rules": SYNTH_RULES[:1]}, headers={"X-Judge-Key": "k"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert tc.get("/health").json()["compile"] == {"model": "kimi-k2", "key": False}


# ── 复核补丁（2026-10-20）：正则安全、coverage、证据极性、applicability、生成端报错 ──

def test_regex_overflow_and_tree_walk_nested_quantifiers():
    code = lambda p: {"kind": "forbidden_regex", "pattern": p, "where": ["body"]}            # noqa: E731
    # 写作台存规则前查的（autowriter validator 的嵌套无上界量词 + deskcore/rulecheck 的「量词只套单字符 / 定长组」）这里都要挡住
    for p in ("a{99999999999}", "((?:ab)+)+", "(a{1,})+", "(?:(?:x+))+", "(a+)+$", "(a+){2,5}", "(哈|哈哈)+$",
              r"^\S+(\s+\S+){5}", r"(品牌[\s\S]*){3}", "(ab?)+", "(?=(a+)+)b", "(a)?(?(1)b|c)"):
        with pytest.raises(R.RuleOutputError):                                               # OverflowError 也变成 RuleOutputError
            R.validate_code(code(p))
    for p in (r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S", "最(好|佳|强|便宜|有效)|第一(名|品牌)", r"品牌[\s\S]*品牌[\s\S]*品牌",
              "(?i)vip", "哈{5,}", "[!！]{3,}", "(?:哈哈)+", "(a|b)+"):
        R.validate_code(code(p))
    # 整条链路：一批里一条正则溢出，只让那一条带 error，不 500、不拖累同批
    rules = [{"id": f"r{i}", "text": f"合成规则{i}", "severity": "soft"} for i in range(16)]

    def gen(prompt, n=1):
        out = [_ok(r["id"]) for r in R.rules_in_prompt(prompt)]
        for o in out:
            if o["id"] == "r7":
                o.update(verdict="code", code=code("a{99999999999}"), coverage="full")
        return [json.dumps({"results": out}, ensure_ascii=False)]

    res = R.compile_rules(gen, rules, batch_size=10, workers=2)
    errs = {r["id"]: r["error"] for r in res if r.get("error")}
    assert list(errs) == ["r7"] and "OverflowError" in errs["r7"]


def test_validate_result_coverage_and_single_char_words():
    V = R.validate_result
    fw = {"kind": "forbidden_word", "words": ["最便宜"]}
    assert V(_ok("x", "code", code=fw))["coverage"] == "full"
    part = V(_ok("x", "code", code=fw, coverage="partial", uncovered="标题字数没查"))
    assert (part["coverage"], part["uncovered"]) == ("partial", "标题字数没查")
    assert V(_ok("x", "code", code=fw, uncovered="多写的"))["uncovered"] == ""               # full 不带 uncovered
    assert V(_ok("x", coverage="full"))["coverage"] is None                                   # not_checkable：没有 coverage
    for raw in (_ok("x", "code", code=fw, coverage=None), _ok("x", "code", code=fw, coverage="most"),
                _ok("x", "code", code=fw, coverage="partial"),                                 # partial 却没写漏了什么
                _ok("x", "code", code={"kind": "forbidden_word", "words": ["最", "最便宜"]})):  # 单字：写作台按包含匹配
        with pytest.raises(R.RuleOutputError):
            V(raw)
    assert R.validate_code({"kind": "required_phrase", "any": ["赞"]})["any"] == ["赞"]          # 必带话术不受单字限制


def test_evidence_follows_pass_if_and_pass_if_yes_never_spends_an_evidence_call():
    from judge import loop as L
    q_yes = {"qid": "a_aaaaaa_1", "ask": "标题有没有点明是讲通勤的？", "pass_if": "是", "scope": "title", "note": ""}
    q_no = {"qid": "b_bbbbbb_1", "ask": "正文有没有提到价格却没注明是活动价？", "pass_if": "否", "scope": "body", "note": ""}
    compiled = [{"id": "a", "sha": "aaaaaa000000", "verdict": "questions", "code": None, "questions": [q_yes]},
                {"id": "b", "sha": "bbbbbb000000", "verdict": "questions", "code": None, "questions": [q_no]}]
    pb = R.to_project_bank(compiled)
    assert {q["id"]: q["evidence"] for q in pb["questions"]} == {"a_aaaaaa_1": "never", "b_bbbbbb_1": "on_true"}
    bank = B.load_bank_data(pb, "desk_rules")
    jev = RecordingJev(pick="单程")                                                             # 两题都答「是」
    dj = L.judge_draft(jev, {"title": TITLE, "body": BODY}, project=bank)
    items = dj.detail["desk_rules"]
    assert items["a_aaaaaa_1"]["answer"] == "是" and not items["a_aaaaaa_1"].get("invalid_reason")   # 守住的答案不会变成 evidence_not_found
    assert items["b_bbbbbb_1"]["evidence"] and "单程" in items["b_bbbbbb_1"]["evidence"]               # 违反的那句
    ev_calls = [b for b in jev.bodies if "说明" in b["state"] and "编号" in b["state"]["说明"]]
    assert len(ev_calls) == 1 and set(ev_calls[0]["questions"]) == {"b_bbbbbb_1"}


def test_applicability_goes_into_prompt_sha_and_where(monkeypatch):
    text = "不要用感叹号"
    assert R.rule_sha(text) == R.rule_sha(text, None) == R.rule_sha(text, "  ")
    assert R.rule_sha(text) == __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()[:12]
    assert R.rule_sha(text, "标题") != R.rule_sha(text) != R.rule_sha(text, "正文")
    rules = [{"id": "t", "text": "别写得太长，控制在20字", "severity": "soft", "applicability": "标题"},
             {"id": "u", "text": "别写得太长，控制在20字", "severity": "soft", "applicability": None}]
    prompt = R.build_prompt(rules)
    assert [r.get("applicability") for r in R.rules_in_prompt(prompt)] == ["标题", None]
    c, H = _client(monkeypatch)
    d = c.post("/compile_rules", json={"project": "ANY", "rules": rules}, headers=H)
    assert d.status_code == 200, d.text
    by = {r["id"]: r for r in d.json()["rules"]}
    assert by["t"]["code"]["where"] == "title" and by["u"]["code"]["where"] == "body"
    assert by["t"]["sha"] == R.rule_sha(rules[0]["text"], "标题") and by["u"]["sha"] == R.rule_sha(rules[1]["text"])


def test_generator_http_error_body_and_refusal_are_reported(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from judge import loop as L
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-0123456789abcdef")
    monkeypatch.delenv("JUDGE_MOCK", raising=False)
    g = R.make_generator()
    body = b'{"error":{"message":"Unknown field output_config (key sk-secret-0123456789abcdef)"}}'

    def bad(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", bad)
    with pytest.raises(L.GeneratorHTTPError) as ei:
        g("p")
    msg = str(ei.value)
    assert "400" in msg and "output_config" in msg and "0123456789abcdef" not in msg
    rule = [{"id": "a", "text": "规则甲", "severity": "soft"}]
    out = R.compile_rules(g, rule)[0]
    assert "output_config" in out["error"] and "生成端调用失败" in out["error"]
    # 拒答：先记用量，再当生成端失败报（不报成「不是合法 JSON」，也不白重试）
    g2 = R.make_generator()
    monkeypatch.setattr(g2, "_post", lambda b: {"content": [], "stop_reason": "refusal", "usage": {"input_tokens": 5, "output_tokens": 0}})
    out2 = R.compile_rules(g2, rule)[0]
    assert "模型拒答" in out2["error"] and "JSON" not in out2["error"] and g2.calls == 1 and g2.usage["input_tokens"] == 5
    g3 = R.make_generator()
    monkeypatch.setattr(g3, "_post", lambda b: {"content": [{"type": "text", "text": '{"results": ['}], "stop_reason": "max_tokens"})
    assert "截断" in R.compile_rules(g3, rule)[0]["error"]
    plain = L.AnthropicCompatGenerator("m", api_key="k")                                      # 别的用法不传 fail_on_stop：照旧返回文本
    monkeypatch.setattr(plain, "_post", lambda b: {"content": [{"type": "text", "text": "半截"}], "stop_reason": "max_tokens"})
    assert plain("p") == ["半截"]
