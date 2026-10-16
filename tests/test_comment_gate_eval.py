"""D-112 gate evaluator: synthetic fixtures only; no network, no real comments."""
import hashlib
import json

import pytest

from scripts import evaluate_comment_gate as G
from scripts import evaluate_comment_reference as E

FIX = json.loads((E.ROOT / "fixtures/comment_candidate4_spec.json").read_text(encoding="utf-8"))


def rows():
    return [{k: n[k] for k in G.SAMPLE_KEYS} for n in FIX["notes"]]


@pytest.fixture
def contract(tmp_path):
    c = json.loads(G.CONTRACT.read_text(encoding="utf-8"))
    rs = rows()
    c["sample"].update(ids=[r["comment_id"] for r in rs], count=len(rs),
                       sha256=hashlib.sha256(G.canonical_sample_bytes(rs)).hexdigest(),
                       md5={r["comment_id"]: [hashlib.md5(r["comment_text"].encode()).hexdigest(),
                                              hashlib.md5(r["raw_content"].encode()).hexdigest()] for r in rs})
    ref = tmp_path / "reference.json"
    ref.write_text(json.dumps({"decision": "D-112", "source_kind": "single_human_reviewer", "ai_suggestions_shown": False,
                               "cases": [{"id": r["comment_id"], "expect": FIX["expected_labels"][i]} for i, r in enumerate(rs)]},
                              ensure_ascii=False), encoding="utf-8")
    c["reference"].update(path=str(ref), sha256=E.sha(ref), frozen=True)
    return c


class Client:
    mock = False

    def __init__(self, override=None):
        self.labels = {r["comment_text"]: FIX["expected_labels"][i] for i, r in enumerate(rows())}
        self.override = override or {}
        self.calls = 0

    def call(self, body):
        self.calls += 1
        exp = dict(self.labels[body["state"]["评论原文"]])
        exp.update(self.override.get(body["state"]["评论原文"], {}))
        return {"answers": {qid: {"noul": .99 if exp[qid] == "是" else .01} if q["type"] == "noul" else
                {"choice": exp[qid], "probabilities": {lab: .99 if lab == exp[qid] else .001 for lab in q["criteria"]}}
                for qid, q in body["questions"].items()}}


def NOCLAIM():
    return None


def test_production_contract_matches_preregistration():
    c = G.load_contract()
    assert c["gating_questions"] == ["speech_act", "names_brand", "echoes_post", "detail_level"]
    assert c["thresholds"]["gating_agreement_min"] == .85 and c["thresholds"]["gating_per_question_min"] == .75
    assert c["thresholds"]["gating_ambiguous_fraction_max"] == .25
    assert c["thresholds"]["unambiguous_endorsement_false_negatives_max"] == 0
    assert c["sample"]["count"] == 40 and len(c["sample"]["ids"]) == 40
    ref = c["reference"]   # 10-16 起参考已冻结: 必须保持冻结, 合同里的 sha 就是仓里这份参考文件的 sha
    assert ref["frozen"] is True and ref["source_kind"] == "single_human_reviewer"
    assert ref["sha256"] == hashlib.sha256((E.ROOT / ref["path"]).read_bytes()).hexdigest()
    assert c["release_accepted"] is False
    assert c["authorization"]["max_live_runs"] == 1


def test_fetch_rebuilds_exact_sample_and_rejects_drift(contract):
    rs = rows()
    comments = [{"comment_id": r["comment_id"], "note_id": r["note_id"], "content": r["comment_text"],
                 "comment_role": r["comment_role"]} for r in rs]
    notes = [{"note_id": r["note_id"], "title": r["title"], "raw_content": r["raw_content"], "project_id": r["project_id"]} for r in rs]
    projects = [{"project_id": p, "category": next(r["category"] for r in rs if r["project_id"] == p)} for p in {r["project_id"] for r in rs}]
    tables = {"comments": comments, "notes": notes, "projects": projects}
    assert G.fetch_sample(contract, url="x", key="y", get=lambda t, p: tables[t]) == rs
    comments[0] = {**comments[0], "content": comments[0]["content"] + "改"}
    with pytest.raises(ValueError):
        G.fetch_sample(contract, url="x", key="y", get=lambda t, p: tables[t])


def test_reference_must_be_frozen_before_any_call(contract):
    contract["reference"]["frozen"] = False
    with pytest.raises(ValueError):
        G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())


def test_perfect_agreement_passes_but_never_accepts_release(contract, tmp_path):
    doc = G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())
    assert G.run(contract, rows(), doc, tmp_path / "out", client=Client(), claim=NOCLAIM) == 0
    s = json.loads((tmp_path / "out/summary.json").read_text())
    assert s["gate"]["passed"] and s["release_accepted"] is False and s["model_calls"] == 20
    raw = (tmp_path / "out/answers.json").read_text() + (tmp_path / "out/summary.json").read_text()
    assert all(r["comment_text"] not in raw for r in rows())  # no raw text in outputs


def test_advisory_misses_do_not_fail_but_gating_and_endorsement_do(contract, tmp_path):
    doc = G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())
    texts = [r["comment_text"] for r in rows()]
    flip_register = {t: {"register": "说不清", "arranged": "是" if FIX["expected_labels"][i]["arranged"] == "否" else "否"}
                     for i, t in enumerate(texts)}
    assert G.run(contract, rows(), doc, tmp_path / "a", client=Client(flip_register), claim=NOCLAIM) == 0
    endorse = [i for i, lab in enumerate(FIX["expected_labels"]) if lab["speech_act"] in G.ENDORSE]
    assert endorse, "fixture needs an endorsement case"
    miss = {texts[endorse[0]]: {"speech_act": "补充经验"}}
    assert G.run(contract, rows(), doc, tmp_path / "b", client=Client(miss), claim=NOCLAIM) == 3
    s = json.loads((tmp_path / "b/summary.json").read_text())
    assert "endorsement_false_negative" in s["gate"]["reasons"]


def test_unresolved_reference_cells_are_excluded_not_filled(contract, tmp_path):
    ref = G.Path(contract["reference"]["path"])
    doc = json.loads(ref.read_text())
    doc["cases"][0]["expect"]["detail_level"] = None
    ref.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    contract["reference"]["sha256"] = E.sha(ref)
    doc = G.load_reference(ref, contract, rows(), G.C.candidate())
    assert G.run(contract, rows(), doc, tmp_path / "out", client=Client(), claim=NOCLAIM) == 0
    s = json.loads((tmp_path / "out/summary.json").read_text())
    assert s["per_question"]["detail_level"]["resolved"] == 19 and s["gating"]["unresolved_reference_cells"] == 1


def test_the_single_run_is_claimed_before_the_first_model_call(contract, tmp_path):
    doc = G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())
    order = []

    class Watching(Client):
        def call(self, body):
            order.append("call")
            return super().call(body)
    assert G.run(contract, rows(), doc, tmp_path / "out", client=Watching(), claim=lambda: order.append("claim")) == 0
    assert order[0] == "claim" and order.count("claim") == 1


def test_an_already_claimed_run_sends_nothing(contract, tmp_path):
    doc = G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())
    client = Client()

    def taken():
        raise G.RunAlreadyClaimed("tag exists")
    assert G.run(contract, rows(), doc, tmp_path / "out", client=client, claim=taken) == 2
    assert client.calls == 0 and not (tmp_path / "out" / G.STARTED_MARKER).exists()


@pytest.mark.parametrize("status", [422, 403, 500])
def test_claim_is_fail_closed(status):
    seen = []
    with pytest.raises(G.RunAlreadyClaimed):
        G.github_claim(repo="o/r", token="t", sha="s", post=lambda url, payload: (seen.append(payload), status)[1])
    assert seen == [{"ref": f"refs/tags/{G.CLAIM_TAG}", "sha": "s"}]


def test_claim_succeeds_only_on_a_fresh_tag_and_needs_credentials(monkeypatch):
    G.github_claim(repo="o/r", token="t", sha="s", post=lambda url, payload: 201)
    for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_SHA"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(G.RunAlreadyClaimed):
        G.github_claim(post=lambda url, payload: 201)


def test_missing_credentials_do_not_use_up_the_run(contract, tmp_path, monkeypatch):
    def refuse():
        raise G.JevError("no key")
    monkeypatch.setattr(G, "JevClient", refuse)
    claimed = []
    doc = G.load_reference(G.Path(contract["reference"]["path"]), contract, rows(), G.C.candidate())
    assert G.run(contract, rows(), doc, tmp_path / "out", claim=lambda: claimed.append(1)) == 4
    assert claimed == [] and not (tmp_path / "out" / G.STARTED_MARKER).exists()


def test_workflow_gives_the_claim_token_to_run_mode_only():
    wf = (E.ROOT / ".github/workflows/comment-gate-eval.yml").read_text(encoding="utf-8")
    assert "contents: write" in wf
    assert "GITHUB_TOKEN: ${{ inputs.mode == 'run' && github.token || '' }}" in wf
    assert "run-started" not in wf and "actions/artifacts" not in wf   # the expiring-artifact guard is gone
