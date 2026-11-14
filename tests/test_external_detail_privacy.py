"""Diagnostic artifacts and errors must not disclose supplier tokens, echoed keys, or content."""
import json

import pytest
import yaml
from pathlib import Path

from judge import external as E

KEY = 'synthetic-provider-credential'
TICKET = 'synthetic-session-ticket'
BODY = '这是纯合成正文，仅供验证诊断输出不包含原文。' * 5


def _provider(raw):
    provider = E.TikHubClient(api_key=KEY, mock=True)
    provider._get = lambda *args: raw
    return provider


def test_successful_details_keep_operational_body_but_only_masked_structure_in_diagnostics():
    raw = {'code': 200, 'data': {'note': {'note_id': 'synthetic-note', 'title': 'synthetic title', 'desc': BODY},
                              'xsec_token': TICKET, 'auth': {'Authorization': 'Bearer ' + KEY},
                              'unknown_echo': 'prefix ' + KEY + ' suffix'}}
    provider = _provider(raw)
    detail, _ = provider.detail('synthetic-note')
    assert detail['desc'] == BODY  # source content remains available to the authorized processing path
    sample = provider.detail_samples[0]['raw']
    encoded = json.dumps(sample, ensure_ascii=False)
    assert all(value not in encoded for value in [KEY, TICKET, BODY, 'synthetic title'])
    assert sample['code'] == 200 and sample['data']['note']['desc'] == {'type': 'str', 'chars': len(BODY)}
    assert sample['data']['xsec_token'] == '***' and sample['data']['auth'] == '***'


@pytest.mark.parametrize('padding', ['', 'x' * 285])
def test_failed_detail_masks_before_error_truncation_and_artifact_serialization(padding):
    # Put the echoed key near the old 300-character cutoff, where truncation-first leaked a prefix.
    raw = {'code': 200, 'data': {'success': False, 'error': padding + KEY,
                               'cookie': TICKET, 'api_key': KEY}}
    provider = _provider(raw)
    with pytest.raises(E.DetailUnparseable) as exc:
        provider.detail('synthetic-note')
    diagnostic_outputs = str(exc.value) + json.dumps(provider.detail_samples, ensure_ascii=False)
    assert KEY not in diagnostic_outputs and TICKET not in diagnostic_outputs
    assert 'synthetic-provider' not in diagnostic_outputs
    assert provider.detail_samples[0]['raw']['data']['success'] is False


def test_recovery_artifacts_have_bounded_retention():
    workflow = yaml.safe_load((Path(__file__).resolve().parent.parent / '.github/workflows/external-corpus.yml').read_text())
    upload = next(step for step in workflow['jobs']['crawl']['steps'] if str(step.get('uses', '')).startswith('actions/upload-artifact@'))
    assert 1 <= upload['with']['retention-days'] <= 7
