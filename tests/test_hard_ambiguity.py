"""Hard-constraint uncertainty must reach clients without becoming an invented violation."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from judge import api as A
from judge import banks as B
from judge import loop as L

ROOT = Path(__file__).resolve().parent.parent
BODY = "这是一段完全虚构的测试稿，只用于验证检查状态的传递，不表示任何真实客户、产品或者发布内容。" * 2


class ProbClient:
    mock = True

    def __init__(self, p):
        self.p, self.calls = p, 0

    def call(self, body):
        self.calls += 1
        answers = {}
        for qid, q in body['questions'].items():
            if q['type'] == 'noul':
                answers[qid] = {'type': 'noul', 'noul': self.p}
            else:  # evidence selection: identify a supplied sentence, never generate evidence
                choice = next(k for k in q['criteria'] if k != '没有')
                answers[qid] = {'type': 'choice', 'choice': choice, 'probabilities': {choice: 0.99}}
        return {'answers': answers, 'usage': {}}


def _bank(evidence='on_true'):
    return B.load_bank_data({'bank_version': 'synthetic-v1', 'questions': [
        {'id': 'constraint', 'type': 'noul', 'instructions': {'zh': '有无合成约束所说的情况？'},
         'criteria': {'true': {'zh': '有'}, 'false': {'zh': '无'}}, 'evidence': evidence},
        {'id': 'description', 'type': 'noul', 'instructions': {'zh': '有无合成描述？'},
         'criteria': {'true': {'zh': '有'}, 'false': {'zh': '无'}}},
    ]}, 'synthetic_project')


@pytest.mark.parametrize('p', [0.35, 0.49, 0.5, 0.51, 0.65])
def test_both_sides_of_ambiguous_hard_rule_require_review_without_repairs(p):
    bank = _bank()
    judged = L.judge_draft(ProbClient(p), {'title': '合成标题', 'body': BODY}, project=bank,
                           hard_rules={(bank.name, 'constraint'): '否'}, judge_paras='never')
    assert judged.hard_ambiguous == [(bank.name, 'constraint')]
    assert not judged.passed() and judged.hard_fails == [] and judged.unjudged == []
    assert L.repair_plan(judged, {bank.name: bank}, target={'constraint': '否'}) == []
    view = L.redact(judged, set())
    assert view['needs_review'] and view['hard_ambiguous'] == [(bank.name, 'constraint')]


def test_description_uncertainty_does_not_veto_draft_and_clear_violation_still_has_plan():
    bank = _bank()
    description = L.judge_draft(ProbClient(0.49), {'body': BODY}, project=bank, judge_paras='never')
    assert description.ambiguous and description.passed() and not description.hard_ambiguous
    violation = L.judge_draft(ProbClient(0.9), {'body': BODY}, project=bank,
                             hard_rules={(bank.name, 'constraint'): '否'}, judge_paras='never')
    assert not violation.passed() and not violation.hard_ambiguous
    assert [p['qid'] for p in L.repair_plan(violation, {bank.name: bank})] == ['constraint']


@pytest.mark.parametrize('p', [0.49, 0.51])
def test_missing_evidence_cannot_turn_an_ambiguous_answer_into_a_certain_violation(p):
    class NoEvidenceClient(ProbClient):
        def call(self, body):
            result = super().call(body)
            for qid, question in body['questions'].items():
                if '没有' in question['criteria']:
                    result['answers'][qid] = {'type': 'choice', 'choice': '没有', 'probabilities': {'没有': 0.99}}
            return result

    # The supported on_choice_except rule with no exemptions requires evidence for either answer.
    bank = _bank(evidence='on_choice_except:')
    judged = L.judge_draft(NoEvidenceClient(p), {'body': BODY}, project=bank,
                           hard_rules={(bank.name, 'constraint'): '否'}, judge_paras='never')
    assert judged.detail[bank.name]['constraint']['invalid_reason'] == 'evidence_not_found'
    assert judged.hard_ambiguous == [(bank.name, 'constraint')] and judged.hard_fails == []
    assert L.redact(judged, set())['needs_review'] and not judged.passed()
    assert L.repair_plan(judged, {bank.name: bank}, target={'constraint': '否'}) == []


def test_default_http_constraints_preserve_review_state_for_writer_without_ledger(monkeypatch):
    monkeypatch.setenv('JUDGE_API_KEY', 'synthetic-admin')
    monkeypatch.setenv('JUDGE_WRITER_API_KEYS', 'synthetic-writer')
    monkeypatch.setattr(A, '_client', lambda: ProbClient(0.49))
    response = TestClient(A.app).post('/judge_draft', headers={'X-Judge-Key': 'synthetic-writer'}, json={
        'title': '合成标题', 'body': BODY, 'project': 'synthetic-project', 'judge_paras': 'never',
        'banks': ['platform_health_v0.1'], 'return_rows': True,
    })
    assert response.status_code == 200
    result = response.json()
    assert result['passed'] is False and result['needs_review'] is True
    assert {tuple(x) for x in result['hard_ambiguous']} == set(L.DEFAULT_HARD_RULES) - {('feature_questions_v0_1', 'efficacy_promise')}
    assert result['hard_fails'] == [] and result['plan'] == [] and result['ledger_rows'] is None


def test_redaction_hides_constraint_id_and_mcp_repair_view_keeps_review_state(monkeypatch):
    from judge import mcp_server as M
    hidden_id = 'synthetic_hidden_question'
    judged = L.DraftJudgement(hard_ambiguous=[('synthetic_bank', hidden_id)], ambiguous=[('synthetic_bank', hidden_id)])
    view = L.redact(judged, {hidden_id})
    assert view['hard_ambiguous'] == [('synthetic_bank', 'hidden')]
    assert hidden_id not in str(view) and view['needs_review'] is True
    monkeypatch.setattr(M, '_config', lambda: None)
    monkeypatch.setattr(M, '_post_json', lambda *args: dict(view, plan=[], recorded=[]))
    result = M.repair_plan_for('合成标题', BODY, project='synthetic-project')
    assert result['hard_ambiguous'] == [('synthetic_bank', 'hidden')] and result['needs_review']
    assert result['passed'] is False and result['plan'] == []


def test_production_loop_stops_on_uncertainty_without_regenerating_or_rejudging():
    bank = _bank()
    client = ProbClient(0.49)
    generation_calls = []

    def generator(prompt, n=1):
        generation_calls.append(n)
        return ['标题：合成标题\n' + BODY] * n

    result = L.produce(client, generator, 'synthetic prompt', k=1, max_repairs=5, project=bank,
                       hard_rules={(bank.name, 'constraint'): '否'})
    assert result['passed'] is False and result['needs_review'] is True
    assert result['hard_ambiguous'] == [(bank.name, 'constraint')] and result['hard_fails'] == []
    assert generation_calls == [1] and client.calls == 1 and len(result['trail']) == 1
