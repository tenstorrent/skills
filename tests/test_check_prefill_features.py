"""Exit-code contract for the prefill features gate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
CHECKER = PLUGIN / 'scripts/check_prefill_features.py'


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


def contract(status='supported', reason=None):
    row = {'item': 'supports_prefix_caching', 'kind': 'capability', 'status': status}
    if status == 'supported':
        row['evidence'] = 'readiness_vllm/prefix_cache_check.json'
    else:
        row['declaration'] = 'supports_prefix_caching=False'
        row['reason'] = reason
    return {'plugin': {'repository': 'tenstorrent/vllm-tt-plugin', 'commit': 'a' * 40},
            'vllm': {'version': 'v0.0.0'}, 'documents_read': ['docs/MODEL_INTERFACE.md'], 'rows': [row]}


def resume(status='pass', control_failed=True, thresholds=None):
    if thresholds is None:
        thresholds = {'pcc_mean_min': 0.99, 'pcc_min_min': 0.98, 'top1_min': 0.98, 'control_min_window': 64}
    return {'status': status, 'reasons': [], 'thresholds': thresholds, 'cases': [
        {'length': 2560, 'start': 128, 'kind': 'lcm_aligned', 'pass': status == 'pass',
         'controls': {'no_history': {'top1_agreement': 0.1 if control_failed else 0.99,
                                     'failed_as_expected': control_failed}}}]}


def burst(ratio=0.4, completed=32, with_reference=True, mean_ratio=None, config_matches=True):
    data = {'completed_requests': completed, 'config': {'num_requests': 32}, 'ttft_ms': {'p99': 1000.0}}
    if with_reference:
        data['serial_reference'] = {'ttft_ratio_p99': ratio, 'ttft_ratio_mean': ratio if mean_ratio is None else mean_ratio,
                                    'config_matches': config_matches}
    return data


def full_evidence(model: Path, **overrides) -> None:
    write(model / 'doc/full_model/serving_contract.json', overrides.get('contract', contract()))
    write(model / 'readiness_vllm/vllm_run_summary.json',
          overrides.get('summary', {'enable_prefix_caching': True, 'apc_log_enabled': True}))
    write(model / 'readiness_vllm/prefix_cache_check.json',
          overrides.get('served', {'status': 'pass', 'reasons': [], 'long_resume': {'checked': True}}))
    write(model / 'readiness/prefill_resume_check.json', overrides.get('resume', resume()))
    if overrides.get('burst', burst()) is not None:
        write(model / 'readiness_vllm/vllm_prefill_burst_benchmark.json', overrides.get('burst', burst()))


def run(model: Path, *extra):
    return subprocess.run([sys.executable, str(CHECKER), '--model-dir', str(model), *extra],
                          text=True, capture_output=True)


def test_missing_contract_is_critical(tmp_path):
    result = run(tmp_path / 'model')
    assert result.returncode == 2
    assert 'Missing serving contract' in result.stderr


def test_complete_supported_evidence_passes(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model)
    result = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert result.returncode == 0, result.stderr


def test_lowered_resume_thresholds_are_advisory_not_silent(tmp_path):
    model = tmp_path / 'model'
    lowered = {'pcc_mean_min': 0.99, 'pcc_min_min': 0.90, 'top1_min': 0.93, 'control_min_window': 64}
    full_evidence(model, resume=resume(thresholds=lowered))
    result = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert result.returncode == 1, result.stderr
    assert 'below the defaults' in result.stderr
    assert "'pcc_min_min': 0.9" in result.stderr and "'top1_min': 0.93" in result.stderr
    record = resume()
    del record['thresholds']
    full_evidence(model, resume=record)
    result = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert result.returncode == 1, result.stderr
    assert 'records no thresholds' in result.stderr


@pytest.mark.parametrize('overrides, message', [
    ({'served': None}, 'prefix_cache_check.json is missing'),
    ({'served': {'status': 'fail', 'reasons': ['warm hit rate 0.0']}}, 'status is'),
    ({'summary': {'enable_prefix_caching': False, 'apc_log_enabled': True}}, 'disabled prefix caching'),
    ({'summary': {'enable_prefix_caching': True, 'apc_log_enabled': False}}, 'automatic prefix caching disabled'),
    ({'resume': resume(control_failed=False)}, 'did not fail'),
    ({'resume': resume(status='fail')}, 'resumed-prefill check status'),
    ({'contract': contract('not_supported', 'prefix caching is not implemented')}, 'without a documented exclusion'),
])
def test_supported_claims_need_passing_evidence(tmp_path, overrides, message):
    model = tmp_path / 'model'
    served = overrides.pop('served', {'status': 'pass', 'reasons': [], 'long_resume': {'checked': True}})
    full_evidence(model, **overrides)
    path = model / 'readiness_vllm/prefix_cache_check.json'
    if served is None:
        path.unlink()
    else:
        write(path, served)
    result = run(model)
    assert result.returncode == 2, result.stderr
    assert message in result.stderr


def test_documented_exclusion_needs_no_prefix_cache_artifacts(tmp_path):
    model = tmp_path / 'model'
    write(model / 'doc/full_model/serving_contract.json',
          contract('not_supported', 'recurrent_state: GDN state is not recoverable from KV blocks'))
    result = run(model, '--stage', 'vllm')
    assert result.returncode == 1, result.stderr
    assert 'prefill_burst' in result.stderr
    write(model / 'readiness_vllm/vllm_prefill_burst_benchmark.json', burst())
    assert run(model, '--stage', 'vllm').returncode == 0


def test_excluded_model_with_a_passing_served_check_is_a_contract_defect(tmp_path):
    model = tmp_path / 'model'
    write(model / 'doc/full_model/serving_contract.json', contract('not_supported', 'block_output model'))
    write(model / 'readiness_vllm/prefix_cache_check.json', {'status': 'pass'})
    write(model / 'readiness_vllm/vllm_prefill_burst_benchmark.json', burst())
    result = run(model)
    assert result.returncode == 2
    assert 'fix the contract' in result.stderr


def test_burst_gate_is_advisory_in_vllm_and_critical_when_required(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model, burst=burst(ratio=0.8))
    advisory = run(model, '--stage', 'vllm')
    assert advisory.returncode == 1, advisory.stderr
    assert 'exceeds 0.6' in advisory.stderr
    required = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert required.returncode == 2
    missing = tmp_path / 'other'
    full_evidence(missing, burst=None)
    assert run(missing, '--require-burst').returncode == 2
    no_reference = tmp_path / 'noref'
    full_evidence(no_reference, burst=burst(with_reference=False))
    result = run(no_reference, '--require-burst')
    assert result.returncode == 2
    assert 'serial_reference' in result.stderr


def test_burst_mean_ratio_and_config_mismatch_are_gated(tmp_path):
    model = tmp_path / 'mean'
    full_evidence(model, burst=burst(ratio=0.4, mean_ratio=0.7))
    result = run(model, '--require-burst')
    assert result.returncode == 2
    assert 'mean TTFT ratio' in result.stderr
    mismatch = tmp_path / 'mismatch'
    full_evidence(mismatch, burst=burst(config_matches=False))
    result = run(mismatch, '--require-burst')
    assert result.returncode == 2
    assert 'different workload config' in result.stderr


def test_plugin_owned_prefix_caching_is_blocked_not_failed(tmp_path):
    model = tmp_path / 'model'
    row = {'item': 'supports_prefix_caching', 'kind': 'capability', 'status': 'plugin_owned',
           'record': 'doc/vllm_integration/plugin_changes.md'}
    data = contract()
    data['rows'] = [row]
    write(model / 'doc/full_model/serving_contract.json', data)
    write(model / 'readiness_vllm/vllm_prefill_burst_benchmark.json', burst())
    missing_record = run(model, '--stage', 'vllm')
    assert missing_record.returncode == 2
    assert 'justification record is missing' in missing_record.stderr
    write(model / 'doc/vllm_integration/plugin_changes.md', '# record\n')
    blocked = run(model, '--stage', 'vllm')
    assert blocked.returncode == 1, blocked.stderr
    assert 'blocked on the plugin change' in blocked.stderr
    full_evidence(model, contract=data)
    write(model / 'doc/vllm_integration/plugin_changes.md', '# record\n')
    assert run(model, '--stage', 'vllm').returncode == 0


def test_served_check_without_long_resume_is_advisory(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model, served={'status': 'pass', 'reasons': [], 'long_resume': {'checked': False}})
    result = run(model, '--stage', 'vllm')
    assert result.returncode == 1, result.stderr
    assert 'one-token resumes' in result.stderr


def test_incomplete_burst_is_always_critical(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model, burst=burst(completed=30))
    result = run(model, '--stage', 'vllm')
    assert result.returncode == 2
    assert 'completed 30 of 32' in result.stderr


def test_unreadable_json_is_a_checker_error(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model)
    write(model / 'readiness_vllm/prefix_cache_check.json', '{not json')
    assert run(model).returncode == 3
    assert run(model, '--stage', 'bogus').returncode == 3
