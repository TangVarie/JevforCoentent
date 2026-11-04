# -*- coding: utf-8 -*-
"""外部语料：Jev 回 200 却缺答 / 答案不在选项里，不能当成「分诊拒绝」或「判完了」（外部评审 2026-11-04 F03）。

以前：分诊那次缺 on_topic / voice → 当 triage_reject 写进 seen，下次不再看；fq 那次缺题 → 20 行 missing 照样入账本、
known_ids 按行数算它写齐了，这篇永远不再重判；每次 Jev 都回 {} → 整次悄悄全拒绝，不红。
现在：这几种都抛 JevIncomplete，上层按单条出错处理（不进 seen、不入账本、下次重判），全部出错时照旧 systemic 红。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.known_external_ids as K  # noqa: E402
from judge import banks as B  # noqa: E402
from judge import core as C  # noqa: E402
from judge import external as E  # noqa: E402
from judge.jev_client import JevClient  # noqa: E402

FQ = ROOT / "banks" / "vendor" / "feature_questions_v0_1.yaml"


def _banks():
    triage = B.load_bank(ROOT / "banks" / "external_triage_v0.1.yaml", name="external_triage_v0.1")
    return triage, B.load_bank(FQ, name="feature_questions_v0_1")


def _cfg(cap: int = 3) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "external_corpus.yaml").read_text(encoding="utf-8"))
    cfg["categories"] = cfg["categories"][:1]; cfg["pages_per_sort"] = 1; cfg["max_keep_per_category_per_run"] = cap
    return cfg


def _kind(body: dict) -> str:
    if "on_topic" in body["questions"]:
        return "triage"
    if str(body["state"].get("说明", "")).startswith("每道题"):
        return "evidence"
    return "fq"


class FlakyJev:
    """mock Jev；break_on(kind, n) 为真的那次调用把答案改坏（kind = triage / fq / evidence，n = 这一类的第几次，从 1 数）。"""
    mock = True

    def __init__(self, break_on, how: str = "drop"):
        self.inner, self.break_on, self.how = JevClient(mock=True), break_on, how
        self.n = Counter()

    def call(self, body):
        r = self.inner.call(body)
        k = _kind(body)
        self.n[k] += 1
        if self.break_on(k, self.n[k]):
            if self.how == "drop":
                r = dict(r, answers={})
            elif self.how == "drop_ad_like":
                r = dict(r, answers={q: a for q, a in r["answers"].items() if q != "ad_like"})
            else:                                                # out_of_vocab：voice 选了不在选项里的值
                ans = dict(r["answers"]); ans["voice"] = {"type": "choice", "choice": "外星人", "probabilities": {"外星人": 0.9}}
                r = dict(r, answers=ans)
        return r


def _incomplete_errors(rep) -> list:
    return [w for w, e in rep.errors if e.startswith("JevIncomplete")]


@pytest.mark.parametrize("how", ["drop", "out_of_vocab"])
def test_triage_without_valid_gate_answer_is_an_error_not_a_reject(how):
    triage, fq = _banks()
    cfg = _cfg()
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(cfg, E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True),
                     FlakyJev(lambda k, n: k == "triage" and n == 1, how), triage, fq, state)
    bad = _incomplete_errors(rep)
    assert len(bad) == 1 and rep.note_errors == 1, rep.errors
    nid = bad[0]
    assert nid not in state["seen"], "没判成不是分诊拒绝：不进 seen，下次还会看"
    assert not any(r["subject_id"] == nid for r in rep.rows)
    assert rep.systemic_failure == "" and rep.judged == 3, "只坏一篇：别的照常判，不红"
    # 第二次（同一份 state，Jev 好了）：这篇重新分诊，有了结局
    rep2 = E.run_once(cfg, E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True), JevClient(mock=True), triage, fq, state)
    assert rep2.errors == [] and nid in state["seen"]


def test_kept_note_with_missing_secondary_triage_answer_is_retried_before_paying_for_full_text():
    triage, fq = _banks()
    cfg = _cfg()

    class CountingDetail(E.TikHubClient):
        details = 0

        def detail(self, note_id, note_type=""):
            self.details += 1
            return super().detail(note_id, note_type)

    prov = CountingDetail(budget=E.Budget(limit_usd=5.0), mock=True)
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(cfg, prov, FlakyJev(lambda k, n: k == "triage", "drop_ad_like"), triage, fq, state)
    assert rep.note_errors > 0 and all(e.startswith("JevIncomplete") and "ad_like=missing" in e for _, e in rep.errors)
    assert prov.details == 0 and rep.rows == [] and rep.kept_notes == [], "四题没判齐就不付费取全文、不入库"
    assert all(v["why"] == "triage_reject" for v in state["seen"].values())


def test_fq_with_missing_answers_writes_no_rows_and_is_rejudged_next_run():
    triage, fq = _banks()
    cfg = _cfg()
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(cfg, E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True),
                     FlakyJev(lambda k, n: k == "fq" and n == 1), triage, fq, state)
    bad = _incomplete_errors(rep)
    assert len(bad) == 1 and "fq 打标没判成" in rep.errors[0][1], rep.errors
    nid = bad[0]
    assert nid not in state["seen"] and nid not in {n["note_id"] for n in rep.kept_notes}
    assert not any(r["subject_id"] == nid for r in rep.rows), "缺题的不入账本（不然 known_ids 会当它写齐了）"
    assert not any(r["invalid_reason"] in E.JEV_INCOMPLETE for r in rep.rows)
    assert rep.systemic_failure == "" and len(rep.rows) == rep.judged * (20 + 4)
    # 第二次（同一份 state，Jev 好了）：这篇重取全文、重判、入账本
    rep2 = E.run_once(cfg, E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True), JevClient(mock=True), triage, fq, state)
    assert state["seen"][nid]["why"] == "kept" and sum(r["subject_id"] == nid for r in rep2.rows) == 24


def test_jev_answering_nothing_for_every_note_is_a_systemic_failure():
    triage, fq = _banks()
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(_cfg(), E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True),
                     FlakyJev(lambda k, n: True), triage, fq, state)
    assert rep.processed > 0 and rep.note_errors == rep.processed and rep.systemic_failure, "整次全「没判成」要红，不能悄悄全拒绝"
    assert state["seen"] == {} and rep.rows == []


# ── known_ids：只数判成了的 fq 题 ──

def _fq_ids() -> list:
    return [q.id for q in _banks()[1].questions]


def test_designed_invalid_reasons_match_the_judge_constants():
    from judge.spans import _tv
    assert K.DESIGNED_INVALID == {_tv.INVALID_NO_TITLE, _tv.INVALID_TEXT_TOO_SHORT, C.INVALID_EVIDENCE_NOT_FOUND, C.INVALID_SPAN_TRUNCATED}
    assert not (K.DESIGNED_INVALID & E.JEV_INCOMPLETE)
    assert K.fq_question_ids() == set(_fq_ids())


def test_missing_rows_do_not_make_a_note_complete():
    ids = _fq_ids()
    rows = ([{"subject_id": "all_missing", "question_id": q, "invalid_reason": "missing"} for q in ids]
            + [{"subject_id": "ok", "question_id": q, "invalid_reason": None} for q in ids]
            + [{"subject_id": "designed", "question_id": q, "invalid_reason": ("evidence_not_found" if i % 2 else "no_title")}
               for i, q in enumerate(ids)]
            + [{"subject_id": "one_oov", "question_id": q, "invalid_reason": ("out_of_vocab" if i == 0 else None)} for i, q in enumerate(ids)]
            # 19 道 fq 判成 + 4 行分诊：原始行数 23 ≥ 20，但 fq 只判成 19 道
            + [{"subject_id": "triage_pad", "question_id": q, "invalid_reason": None} for q in ids[:-1]]
            + [{"subject_id": "triage_pad", "question_id": q, "invalid_reason": None} for q in ("on_topic", "voice", "ad_like", "has_product")]
            # 同一题两个 extractor 各一行：只算一题
            + [{"subject_id": "dup", "question_id": ids[0], "invalid_reason": None}] * 20)
    counts = K.valid_counts(rows, set(ids))
    done, short = K.complete_ids(["all_missing", "ok", "designed", "one_oov", "triage_pad", "dup"], counts, need=len(ids))
    assert done == ["ok", "designed"] and short == ["all_missing", "one_oov", "triage_pad", "dup"]


def test_main_skips_notes_whose_ledger_is_all_missing(monkeypatch, tmp_path):
    ids = _fq_ids()

    def fake_get(url, key, path):
        if path.startswith("external_notes?"):
            return [] if "offset=1000" in path else [{"note_id": "n1"}, {"note_id": "n2"}]
        if path.startswith("note_feature_answers?"):
            assert "select=subject_id,question_id,invalid_reason" in path
            if "offset=1000" in path:
                return []
            return ([{"subject_id": "n1", "question_id": q, "invalid_reason": None} for q in ids]
                    + [{"subject_id": "n2", "question_id": q, "invalid_reason": "missing"} for q in ids])
        raise AssertionError(path)
    monkeypatch.setattr(K, "_get", fake_get)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    out = tmp_path / "known.txt"
    assert K.main([str(out)]) == 0
    assert out.read_text(encoding="utf-8").split() == ["n1"], "n2 的 20 行全是 missing：不算写齐，下次重抓重判"


class OneSidedTriageJev(FlakyJev):
    """分诊那次只回一个判成的、决定性的拒绝答案，另一题缺；fq / 证据照常。"""

    def __init__(self, answers: dict):
        super().__init__(lambda k, n: False)
        self.answers = answers

    def call(self, body):
        r = super().call(body)
        return dict(r, answers=dict(self.answers)) if _kind(body) == "triage" else r


@pytest.mark.parametrize("answers", [
    {"on_topic": {"type": "noul", "noul": 0.05}},                                             # 判成「否」、voice 缺
    {"voice": {"type": "choice", "choice": "商家或品牌", "probabilities": {"商家或品牌": 0.9}}},  # 判成不留的视角、on_topic 缺
    {"on_topic": {"type": "noul", "noul": 0.05}, "voice": {"type": "choice", "choice": "外星人", "probabilities": {"外星人": 0.9}}},
])
def test_one_valid_decisive_reject_is_a_reject_even_if_the_other_gate_answer_is_missing(answers):
    triage, fq = _banks()
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(_cfg(), E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True), OneSidedTriageJev(answers),
                     triage, fq, state)
    assert rep.errors == [] and rep.note_errors == 0 and rep.processed > 0 and rep.judged == 0
    assert state["seen"] and all(v["why"] == "triage_reject" for v in state["seen"].values())


def test_on_topic_yes_with_voice_missing_is_still_not_judged():
    triage, fq = _banks()
    state = {"seen": {}, "monthly": {}}
    rep = E.run_once(_cfg(), E.TikHubClient(budget=E.Budget(limit_usd=5.0), mock=True),
                     OneSidedTriageJev({"on_topic": {"type": "noul", "noul": 0.95}}), triage, fq, state)
    assert rep.processed > 0 and rep.note_errors == rep.processed and state["seen"] == {}
    assert all(e.startswith("JevIncomplete") and "voice=missing" in e for _, e in rep.errors)
