"""Exit-code contract for the prefill features gate."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

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
    control = {'top1_agreement': 0.1 if control_failed else 0.99, 'failed_as_expected': control_failed}
    return {'status': status, 'reasons': [], 'thresholds': thresholds, 'cases': [
        {'length': 2560, 'start': 128, 'kind': 'lcm_aligned', 'pass': status == 'pass',
         'controls': {'no_history': dict(control), 'fresh_suffix': dict(control)}}]}


def cold_cache(hit_rate=0.0):
    return {'reset': True, 'queries': 4096, 'hits': int(4096 * hit_rate), 'hit_rate': hit_rate}


def burst(ratio=0.4, completed=32, with_reference=True, mean_ratio=None, config_matches=True,
          burst_cache=cold_cache(), serial_cache=cold_cache()):
    data = {'completed_requests': completed, 'config': {'num_requests': 32}, 'ttft_ms': {'p99': 1000.0}}
    if burst_cache is not None:
        data['prefix_cache'] = burst_cache
    if with_reference:
        data['serial_reference'] = {'ttft_ratio_p99': ratio, 'ttft_ratio_mean': ratio if mean_ratio is None else mean_ratio,
                                    'config_matches': config_matches}
        if serial_cache is not None:
            data['serial_reference']['serial_prefix_cache'] = serial_cache
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


@pytest.fixture
def prefill_probe(monkeypatch):
    # Load the real probe and counter parser with only HTTP and model dependencies stubbed.
    package = ModuleType('readiness_check')
    http = SimpleNamespace(get=Mock(), post=Mock(return_value=SimpleNamespace(status_code=404)))
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, 'requests', http)
        imports.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=None))
        imports.setitem(sys.modules, 'readiness_check', package)
        for name in ('prefix_cache_check', 'prefill_burst'):
            spec = importlib.util.spec_from_file_location(
                name, PLUGIN / 'runtime/readiness_check' / f'{name}.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setattr(package, name, module)
    return package.prefill_burst, http


@pytest.mark.parametrize('metrics', [
    '',
    'vllm:num_requests_running 0\n',
    'vllm:prefix_cache_queries_total 0\n',
    'vllm:prefix_cache_hits_total 0\n',
    'vllm:prefix_cache_queries_total 0\nvllm:prefix_cache_hits_total 0\n',
])
@pytest.mark.parametrize('scrape', [0, 1])
@pytest.mark.parametrize('arm', ['burst', 'serial'])
def test_burst_gate_requires_observed_cache_counters(tmp_path, prefill_probe, metrics, scrape, arm):
    probe, http = prefill_probe
    zero_counters = 'vllm:prefix_cache_queries_total 0\nvllm:prefix_cache_hits_total 0\n'
    snapshots = [zero_counters, zero_counters]
    snapshots[scrape] = metrics
    http.get.side_effect = [SimpleNamespace(text=text, raise_for_status=Mock()) for text in snapshots]
    measured = probe.run_with_prefix_cache_state('http://server', lambda: {})
    model = tmp_path / 'model'
    full_evidence(model, burst=burst(**{f'{arm}_cache': measured['prefix_cache']}))
    result = run(model, '--require-burst')
    if metrics == zero_counters:
        assert result.returncode == 0, result.stderr
        assert measured['prefix_cache'] == {'reset': False, 'queries': 0, 'hits': 0, 'hit_rate': None}
    else:
        assert result.returncode == 2, result.stderr
        assert f'{arm} arm has no cold-cache evidence' in result.stderr
        assert measured['prefix_cache'] == {'reset': False, 'queries': None, 'hits': None, 'hit_rate': None}


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


def test_burst_gate_is_advisory_at_both_stages_unless_burst_critical(tmp_path):
    model = tmp_path / 'model'
    full_evidence(model, burst=burst(ratio=0.8))
    advisory = run(model, '--stage', 'vllm')
    assert advisory.returncode == 1, advisory.stderr
    assert 'exceeds 0.6' in advisory.stderr
    # D5: the Stage 10 check script still passes --require-burst; the ratio is recorded, not required.
    stage10 = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert stage10.returncode == 1, stage10.stderr
    assert 'advisory: prefill burst p99 TTFT ratio' in stage10.stderr
    assert run(model, '--stage', 'optimized-vllm', '--burst-critical').returncode == 2
    missing = tmp_path / 'other'
    full_evidence(missing, burst=None)
    result = run(missing, '--stage', 'optimized-vllm', '--require-burst')
    assert result.returncode == 1, result.stderr
    assert 'vllm_prefill_burst_benchmark.json is missing' in result.stderr
    assert run(missing, '--burst-critical').returncode == 2
    no_reference = tmp_path / 'noref'
    full_evidence(no_reference, burst=burst(with_reference=False))
    assert run(no_reference, '--require-burst').returncode == 1
    result = run(no_reference, '--burst-critical')
    assert result.returncode == 2
    assert 'serial_reference' in result.stderr


def test_burst_mean_ratio_and_config_mismatch_are_gated(tmp_path):
    model = tmp_path / 'mean'
    full_evidence(model, burst=burst(ratio=0.4, mean_ratio=0.7))
    assert run(model, '--require-burst').returncode == 1
    result = run(model, '--burst-critical')
    assert result.returncode == 2
    assert 'mean TTFT ratio' in result.stderr
    mismatch = tmp_path / 'mismatch'
    full_evidence(mismatch, burst=burst(config_matches=False))
    result = run(mismatch, '--burst-critical')
    assert result.returncode == 2
    assert 'different workload config' in result.stderr


def test_enabled_batched_prefill_needs_batched_equals_solo_evidence(tmp_path):
    data = contract()
    data['rows'].append({'item': 'batched_prefill', 'kind': 'call', 'status': 'supported',
                         'evidence': 'readiness/batched_equals_solo.log'})
    model = tmp_path / 'model'
    full_evidence(model, contract=data)
    result = run(model, '--stage', 'optimized-vllm', '--require-burst')
    assert result.returncode == 2, result.stderr
    assert 'batched-equals-solo evidence readiness/batched_equals_solo.log does not exist' in result.stderr
    write(model / 'readiness/batched_equals_solo.log', 'all rows bit-equal\n')
    assert run(model, '--stage', 'optimized-vllm', '--require-burst').returncode == 0
    data['rows'][-1] = {'item': 'batched_prefill', 'kind': 'call', 'status': 'not_supported',
                        'declaration': 'per-row prefill', 'reason': 'optional (D5)'}
    off = tmp_path / 'off'
    full_evidence(off, contract=data)
    assert run(off, '--stage', 'optimized-vllm', '--require-burst').returncode == 0


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


def test_malformed_artifact_schemas_are_checker_errors(tmp_path):
    listed = tmp_path / 'listed'
    full_evidence(listed, summary=[])
    result = run(listed)
    assert result.returncode == 3, result.stderr
    assert 'vllm_run_summary.json: top level must be a JSON dict' in result.stderr
    assert 'Traceback' not in result.stderr
    bad_case = resume()
    bad_case['cases'] = ['not a case']
    cased = tmp_path / 'cased'
    full_evidence(cased, resume=bad_case)
    result = run(cased)
    assert result.returncode == 3, result.stderr
    assert 'cases[0] must be a JSON dict' in result.stderr


def test_final_resume_evidence_needs_both_negative_controls(tmp_path):
    one = resume()
    del one['cases'][0]['controls']['fresh_suffix']
    model = tmp_path / 'one-control'
    full_evidence(model, resume=one)
    result = run(model)
    assert result.returncode == 2, result.stderr
    assert "without ['fresh_suffix']" in result.stderr
    narrow = resume()
    narrow['cases'].append({'length': 400, 'start': 399, 'kind': 'last_token', 'pass': True, 'controls': {}})
    both = tmp_path / 'narrow-extra'
    full_evidence(both, resume=narrow)
    assert run(both).returncode == 0


def test_burst_arms_must_run_on_a_cold_prefix_cache(tmp_path):
    warm = tmp_path / 'warm'
    full_evidence(warm, burst=burst(burst_cache=cold_cache(0.9)))
    result = run(warm, '--stage', 'vllm')
    assert result.returncode == 2, result.stderr
    assert 'burst arm measured with a warm prefix cache' in result.stderr
    warm_serial = tmp_path / 'warm-serial'
    full_evidence(warm_serial, burst=burst(serial_cache=cold_cache(0.5)))
    result = run(warm_serial, '--require-burst')
    assert result.returncode == 2
    assert 'serial arm measured with a warm prefix cache' in result.stderr
    absent = tmp_path / 'absent'
    full_evidence(absent, burst=burst(burst_cache=None, serial_cache=None))
    result = run(absent, '--require-burst')
    assert result.returncode == 1, result.stderr
    assert 'no prefix-cache state for the burst arm' in result.stderr
    assert 'no prefix-cache state for the serial arm' in result.stderr
    cold = tmp_path / 'cold'
    full_evidence(cold)
    assert run(cold, '--require-burst').returncode == 0


def test_burst_arm_without_cold_cache_evidence_is_critical(tmp_path):
    unknown = {'reset': False, 'queries': None, 'hits': None, 'hit_rate': None}
    model = tmp_path / 'unknown'
    full_evidence(model, burst=burst(burst_cache=unknown))
    result = run(model, '--require-burst')
    assert result.returncode == 2, result.stderr
    assert 'burst arm has no cold-cache evidence' in result.stderr
    reset_only = tmp_path / 'reset-only'
    full_evidence(reset_only, burst=burst(burst_cache={'reset': True, 'queries': None, 'hits': None, 'hit_rate': None}))
    assert run(reset_only, '--require-burst').returncode == 0
    apc_off = tmp_path / 'apc-off'
    full_evidence(apc_off, burst=burst(serial_cache={'reset': False, 'queries': 0, 'hits': 0, 'hit_rate': None}))
    assert run(apc_off, '--require-burst').returncode == 0
