from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from judge import api as A
from judge.jev_client import JevError


POST = {"project": "TUGE", "post_title": "戒烟第三天", "post_body": "戒烟第三天，嘴里没味，靠嗑瓜子撑着。", "kind": "product"}
SLOT = {"id": "q1", "speech_act": ["提问"], "must_echo": False,
        "value_ok": ["可行动信息", "判断依据", "无"], "min_detail": "无"}


class Answers:
    def __init__(self):
        self.calls = []
        self.omit = set()
        self.overrides = {}
        self.fail = False
        self.ambiguous = set()

    def call(self, body):
        self.calls.append(body)
        if self.fail:
            raise JevError("test outage")
        wanted = {"speech_act": "提问", "register": "随手口语", "reader_value": "无",
                  "detail_level": "无", "comment_intent": "补充信息", "praise_share": "少数"}
        bools = {"echoes_post": True, "has_friction": True}
        out = {}
        for qid, q in body["questions"].items():
            if qid in self.omit:
                continue
            if q["type"] == "noul":
                value = self.overrides.get(qid, bools.get(qid, False))
                out[qid] = {"type": "noul", "noul": 0.52 if qid in self.ambiguous else (0.95 if value else 0.05)}
            else:
                labels = list(q["criteria"])
                value = self.overrides.get(qid, wanted.get(qid, labels[0]))
                out[qid] = {"type": "choice", "choice": value,
                            "probabilities": {k: 0.95 if k == value else 0.01 for k in labels}}
        return {"answers": out, "usage": {"input_tokens": 1}}


@pytest.fixture
def wired(monkeypatch, policy_cfg):
    monkeypatch.setenv("JUDGE_API_KEY", "admin")
    monkeypatch.setenv("JUDGE_WRITER_API_KEYS", "writer")
    monkeypatch.setenv("JUDGE_COMMENT_TOOLS_ENABLED", "1")
    jev = Answers()
    monkeypatch.setattr(A, "JevClient", lambda **kw: jev)
    return TestClient(A.app), jev


def single(client, **changes):
    return client.post("/comment_repair_plan_for", headers={"X-Judge-Key": "writer"},
                       json={**POST, "text": "怎么买呢？", "slot": SLOT, **changes})


def test_writer_key_gets_real_comment_profile_and_unpublished_policy(wired):
    client, jev = wired
    r = single(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["passed"] is True and data["profile"]["speech_act"] == "提问"
    assert data["policy"]["published"] is False
    assert "ledger_rows" not in data and "written" not in data
    assert len(jev.calls) == 2


def test_slot_failure_returns_an_actionable_plan(wired):
    client, jev = wired
    jev.overrides["names_brand"] = True   # 门槛题：提问位不许点名
    data = single(client).json()
    assert data["passed"] is False
    assert any(p["qid"] == "names_brand" and p["definition"] for p in data["plan"])


@pytest.mark.parametrize("qid,value", [("register", "文案腔"), ("reader_value", "只有评价"), ("arranged", True),
                                       ("comment_intent", "引导私信")])
def test_advisory_questions_only_hint(wired, qid, value):
    """D-112 验收口径：只提示的题不合要求时进 hints，不进 plan，不影响 passed。"""
    client, jev = wired
    jev.overrides[qid] = value
    data = single(client, slot={**SLOT, "value_ok": ["可行动信息"], "intent": "补充信息"} if qid == "reader_value"
                  else {**SLOT, "intent": "补充信息"}).json()
    assert data["passed"] is True and data["needs_review"] is False, data
    assert not any(p["qid"] == qid for p in data["plan"])
    assert any(h["qid"] == qid for h in data["hints"]), data["hints"]
    assert qid in data["advisory"] and not data["hard_fails"]


def test_arranged_hint_is_kept_alongside_gate_failures(wired):
    client, jev = wired
    jev.overrides.update(names_brand=True, arranged=True)
    data = single(client).json()
    assert data["passed"] is False and [p["qid"] for p in data["plan"]] == ["names_brand"]
    assert [h["qid"] for h in data["hints"]] == ["arranged"]


@pytest.mark.parametrize("how", ["ambiguous", "missing"])
def test_unaccepted_ops_questions_never_gate(wired, how):
    """运营侧题库整套没验收过：is_scripted 标歧义或漏答都不送复核、不判不过（D-112 口径）。"""
    client, jev = wired
    if how == "ambiguous":
        jev.ambiguous = {"is_scripted"}
    else:
        jev.omit.add("is_scripted")
    data = single(client, slot={**SLOT, "intent": "补充信息"}).json()
    assert data["passed"] is True and data["needs_review"] is False, data


def test_every_ops_question_is_advisory():
    from judge.comment_api import ADVISORY
    ops = A.get_bank("comment_ops_v0.2")
    assert {q.id for q in ops.questions} <= ADVISORY


def test_reader_bank_is_the_accepted_c4(wired):
    client, jev = wired
    single(client)
    from judge.comment_api import READER_BANK
    bank = A.get_bank(READER_BANK)
    assert bank.version == "cb-v0.6-candidate4"
    assert bank.sha256 == "a0c04acf37cb05005ed2f3c01ae3b0410221a392da39b665b25879a678cd534d"


def test_single_ambiguity_routes_to_review(wired):
    client, jev = wired
    jev.ambiguous = {"names_brand"}
    data = single(client, slot={**SLOT, "may_name_brand": True}).json()
    assert "names_brand" in data["ambiguous"] and data["passed"] is False and data["needs_review"] is True


def test_thread_ambiguity_routes_to_review(wired):
    client, jev = wired
    jev.ambiguous = {"has_friction"}
    data = client.post("/judge_thread", headers={"X-Judge-Key": "writer"},
                       json={**POST, "comments": [{"text": "怎么买呢"}]}).json()
    assert "has_friction" in data["ambiguous"] and data["needs_review"] is True and data["passed"] is False


def test_thread_uses_slot_author_role_in_both_judgments(wired):
    client, jev = wired
    r = client.post("/judge_thread", headers={"X-Judge-Key": "writer"}, json={**POST, "comments": [
        {"text": "怎么买呢", "slot": {**SLOT, "role": "贴主"}}]})
    assert r.status_code == 200
    reader_call = next(c for c in jev.calls if "speech_act" in c["questions"])
    thread_call = next(c for c in jev.calls if "has_friction" in c["questions"])
    assert reader_call["state"]["评论角色"] == "贴主"
    assert "[贴主]" in thread_call["state"]["评论列表"]


def test_advisory_ambiguity_is_reported_but_not_routed_to_review(wired):
    """运营侧 comment_intent 选了「其他」照样标歧义，但它只提示，不送复核（D-112）；门槛题歧义仍送复核（上面那条）。"""
    client, jev = wired
    jev.overrides["comment_intent"] = "其他"
    data = single(client).json()
    assert "comment_intent" in data["ambiguous"] and data["needs_review"] is False and data["passed"] is True


@pytest.mark.parametrize("which", ["missing", "endorsement"])
def test_missing_answers_and_endorsements_do_not_pass(wired, which):
    client, jev = wired
    if which == "missing":
        jev.omit.add("echoes_post")   # 门槛题漏答
    else:
        jev.overrides["speech_act"] = "亲历背书"
    data = single(client).json()
    assert data["needs_review"] is True and data["passed"] is False


def test_thread_propagates_individual_review_and_checks_whole_group(wired):
    client, jev = wired
    jev.overrides["speech_act"] = "亲历背书"
    r = client.post("/judge_thread", headers={"X-Judge-Key": "writer"},
                    json={**POST, "comments": [{"text": "我用过", "role": "读者位"}, {"text": "谢谢", "role": "贴主"}]})
    data = r.json()
    assert r.status_code == 200 and data["passed"] is False and data["needs_review"] is True
    assert data["code"]["endorse"] == 2 and len(data["comments"]) == 2
    assert len(jev.calls) == 5


@pytest.mark.parametrize("change", [{"project": "OKMAN"}, {"category": "处方药"}])
def test_policy_rejects_before_any_jev_call(wired, change):
    client, jev = wired
    assert single(client, **change).status_code == 403
    assert jev.calls == []


@pytest.mark.parametrize("change", [{"write": True}, {"published": True}, {"project": " "},
                                     {"slot": {**SLOT, "speech_act": ["编造体验"]}}, {"text": " "}])
def test_invalid_or_relaxing_inputs_rejected_without_spending(wired, change):
    client, jev = wired
    assert single(client, **change).status_code == 422
    assert jev.calls == []


def test_auth_and_default_gold_gate_are_fail_closed(wired, monkeypatch):
    client, jev = wired
    r = client.post("/comment_repair_plan_for", json={**POST, "text": "怎么买", "slot": SLOT})
    assert r.status_code == 401 and jev.calls == []
    monkeypatch.delenv("JUDGE_COMMENT_TOOLS_ENABLED")
    assert single(client).status_code == 503 and jev.calls == []
    assert client.get("/health").json()["comment_tools_enabled"] is False


def test_failure_is_not_a_successful_comment_check(wired):
    client, jev = wired
    jev.fail = True
    assert single(client).status_code == 502


def test_missing_advisory_answer_is_flagged_not_reviewed(wired):
    client, jev = wired
    jev.omit.add("arranged")
    data = single(client).json()
    assert data["passed"] is True and data["needs_review"] is False and "参考题漏答" in data["flags"]
