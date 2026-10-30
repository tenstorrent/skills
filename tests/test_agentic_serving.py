"""Offline tests for the agentic-serving gate and benchmark prefix-caching evidence."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins/tt-model-bringup'
sys.path.insert(0, str(PLUGIN / 'runtime'))
from benchmark_stage.evidence import validate_server

CHECKER = PLUGIN / 'scripts/check_agentic_serving.py'


def evidence():
    return {
        'workload': {'target_concurrency': 16, 'working_set_tokens': 20000, 'source': 'estimated'},
        'ledger': {'weights': [{'name': 'attention', 'bytes': 1, 'copies': 1}], 'pool_tokens': 400000},
        'serving': {'prefix_caching': True, 'hit_fraction_of_ceiling': 0.95,
                    'trace_captures_after_warmup': {'captures': 0, 'retirements': 0}},
    }


def check(model, stage, data):
    (model / 'doc').mkdir(parents=True, exist_ok=True)
    (model / 'doc/agentic_serving.json').write_text(json.dumps(data))
    result = subprocess.run([sys.executable, str(CHECKER), '--model-dir', str(model), '--stage', stage],
                            text=True, capture_output=True)
    return result.returncode, result.stderr


@pytest.mark.parametrize('stage', ['full-model', 'vllm', 'optimized-vllm'])
def test_valid_evidence_passes(tmp_path, stage):
    assert check(tmp_path, stage, evidence()) == (0, '')


def test_missing_evidence_is_critical(tmp_path):
    result = subprocess.run([sys.executable, str(CHECKER), '--model-dir', str(tmp_path), '--stage', 'full-model'],
                            text=True, capture_output=True)
    assert result.returncode == 2


@pytest.mark.parametrize('stage, mutate, reason', [
    ('full-model', lambda e: e['ledger']['weights'][0].update(copies=2), ('ledger', 'weights', 0, 'copy_reason')),
    ('full-model', lambda e: e['ledger'].update(pool_tokens=1000), ('ledger', 'pool_shortfall_reason')),
    ('vllm', lambda e: e['serving'].update(prefix_caching=False), ('serving', 'reason')),
    ('vllm', lambda e: e['serving'].update(hit_fraction_of_ceiling=0.5), ('serving', 'reason')),
    ('vllm', lambda e: e['serving']['trace_captures_after_warmup'].update(retirements=4), ('serving', 'trace_reason')),
])
def test_shortfall_is_critical_without_a_reason_and_advisory_with_one(tmp_path, stage, mutate, reason):
    data = evidence()
    mutate(data)
    assert check(tmp_path, stage, data)[0] == 2
    target = data
    for key in reason[:-1]:
        target = target[key]
    target[reason[-1]] = 'measured: recorded cause'
    code, stderr = check(tmp_path, stage, data)
    assert code == 1 and 'recorded cause' in stderr


CHECKS = ('time_split', 'busy_rows', 'live_prefill_rows', 'trace_captures', 'served_step', 'prefix_hits',
          'cache_pressure', 'cache_bytes', 'memory_headroom', 'cutoffs', 'eval_validity', 'accuracy_drift')
CALCULATIONS = ('batch', 'suite_hours', 'agent_timeout', 'output_cap', 'bytes', 'agents', 'block_cost',
                'prefill_padding')


def goal12(model, **overrides):
    """Write a complete, passing goal-12 evidence set; overrides replace whole files."""
    evals = model / 'doc/agentic_evals'
    (evals / 'full/swe').mkdir(parents=True, exist_ok=True)
    files = {
        'plan.json': {},
        'REPORT.md': '# Report',
        'estimate.json': {'evals': {'swe': {'decision': 'go', 'reason': 'fits'}}},
        'full/swe/results.json': {'actual_hours': 6.5},
        'survey.json': {'checks': {k: {'value': 1, 'healthy': True} for k in CHECKS},
                        'calculations': {k: 1 for k in CALCULATIONS}, 'shortfalls': []},
        'preflight.json': {k: {'ok': True} for k in ('registry', 'disk', 'release_route', 'isolation_smoke')},
        'efforts.json': {
            'efforts': [
                {'problem': 'padded prefill', 'owner': 'prefill', 'status': 'done',
                 'goal': 'prefill one live row at batch-1 cost', 'plan': 'runs/prefill/plan.md',
                 'predicted': {'saving_hours': {'swe': 3.0, 'terminal': 1.5}, 'work_hours': 2, 'basis': 'calculated'},
                 'confirm': {'method': 'prefill vs batch shape', 'success': 'one live row costs batch-1 time'},
                 'box_hours': 4, 'used_hours': 3, 'change': 'kept', 'problem_status': 'solved',
                 'confirmed': 'prefill 0.9 s per live row', 'evidence': 'runs/prefill'},
                {'problem': 'block size', 'status': 'not_run', 'reason': 'below the budget line',
                 'predicted': {'saving_hours': {'swe': 0.5}, 'work_hours': 3, 'basis': 'guess'},
                 'confirm': {'method': 'cache replay', 'success': 'hit rate above 0.9'}},
            ],
            'rankings': [{'after': 'survey', 'order': [
                {'problem': 'padded prefill', 'saving_hours': 4.5, 'work_hours': 2},
                {'problem': 'block size', 'saving_hours': 0.5, 'work_hours': 3}]}],
        },
        'serving_config.json': {'agent_timeout_s': 6300, 'request_timeout_s': 3600, 'output_cap_tokens': 81920,
                                'release_limits': {'agent_timeout_s': 6300, 'request_timeout_s': 3600,
                                                   'output_cap_tokens': 81920},
                                'validated_batches': [1, 8, 16, 32],
                                'batch_by_eval': {'swe': {'batch': 8, 'planned_clients': 8}}},
    }
    files.update(overrides)
    for name, body in files.items():
        if body is None:
            (evals / name).unlink(missing_ok=True)
        else:
            (evals / name).write_text(body if isinstance(body, str) else json.dumps(body))
    return files


def test_agentic_evals_requires_decisions_and_results(tmp_path):
    goal12(tmp_path, **{'full/swe/results.json': None})
    assert check(tmp_path, 'agentic-evals', evidence())[0] == 2
    goal12(tmp_path, **{'estimate.json': {'evals': {
        'swe': {'decision': 'go', 'reason': 'fits'}, 'terminal': {'decision': 'stop', 'reason': 'latest start passed'}}}})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 1 and 'latest start passed' in stderr


def test_complete_goal12_evidence_passes(tmp_path):
    goal12(tmp_path)
    assert check(tmp_path, 'agentic-evals', evidence()) == (0, '')


@pytest.mark.parametrize('name', ['survey.json', 'preflight.json', 'efforts.json'])
def test_goal12_requires_survey_preflight_and_efforts(tmp_path, name):
    goal12(tmp_path, **{name: None})
    assert check(tmp_path, 'agentic-evals', evidence())[0] == 2


def test_survey_must_answer_every_check(tmp_path):
    files = goal12(tmp_path)
    survey = files['survey.json']
    del survey['checks']['live_prefill_rows']
    goal12(tmp_path, **{'survey.json': survey})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and 'live_prefill_rows' in stderr
    survey['checks']['live_prefill_rows'] = {'not_measured': 'needs the prefill-shape test'}
    goal12(tmp_path, **{'survey.json': survey})
    assert check(tmp_path, 'agentic-evals', evidence()) == (0, '')


def test_carried_shortfalls_must_be_priced(tmp_path):
    data = evidence()
    data['ledger']['weights'][0].update(copies=2, copy_reason='measured: fused copy')
    files = goal12(tmp_path)
    code, stderr = check(tmp_path, 'agentic-evals', data)
    assert code == 2 and 'priced in suite hours' in stderr
    survey = files['survey.json']
    survey['shortfalls'] = [{'name': 'attention held twice', 'suite_hours': 1.5}]
    goal12(tmp_path, **{'survey.json': survey})
    assert check(tmp_path, 'agentic-evals', data)[0] == 1


def test_failed_release_route_preflight_is_critical(tmp_path):
    files = goal12(tmp_path)
    preflight = files['preflight.json']
    preflight['release_route'] = {'ok': False, 'error': 'GHCR 403'}
    goal12(tmp_path, **{'preflight.json': preflight})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and 'release_route' in stderr


def efforts_with(tmp_path, change):
    files = goal12(tmp_path)
    ledger = files['efforts.json']
    change(ledger)
    goal12(tmp_path, **{'efforts.json': ledger})
    return check(tmp_path, 'agentic-evals', evidence())


def test_effort_over_its_box_without_a_re_estimate_is_advisory(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].update(used_hours=9))
    assert code == 1 and '4 h box' in stderr


def test_effort_over_its_box_with_a_re_estimate_passes(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].update(used_hours=9, re_estimate='2 h left'))
    assert code == 0, stderr


@pytest.mark.parametrize('field', ['predicted', 'confirm'])
def test_every_effort_needs_a_prediction_and_a_confirmation(tmp_path, field):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][1].pop(field))
    assert code == 2 and 'block size' in stderr


@pytest.mark.parametrize('field', ['goal', 'plan'])
def test_effort_that_ran_needs_its_own_goal_and_plan(tmp_path, field):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].pop(field))
    assert code == 2 and 'goal and plan' in stderr


def test_kept_change_with_open_problem_passes(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].update(problem_status='open'))
    assert code == 0, stderr


def test_done_effort_needs_a_confirmed_result(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].pop('confirmed'))
    assert code == 2 and 'confirmed' in stderr


def test_effort_that_did_not_run_needs_a_reason(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][1].pop('reason'))
    assert code == 2 and 'did not run' in stderr


def test_rankings_are_required(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e.pop('rankings'))
    assert code == 2 and 'rankings' in stderr


def test_reordered_ranking_needs_a_reason(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['rankings'][0]['order'].reverse())
    assert code == 2 and 'no reason' in stderr

    def reorder_with_reason(ledger):
        ledger['rankings'][0]['order'].reverse()
        ledger['rankings'][0]['reason'] = 'block size unblocks the cache effort'

    code, stderr = efforts_with(tmp_path, reorder_with_reason)
    assert code == 0, stderr


@pytest.mark.parametrize('mutate, message', [
    (lambda c: c.update(agent_timeout_s=900), 'agent_timeout_s 900 is below'),
    (lambda c: c.update(output_cap_tokens=16384), 'output_cap_tokens 16384 is below'),
    (lambda c: c['batch_by_eval']['swe'].update(batch=32), 'batch 8 is validated'),
])
def test_release_limits_and_batch_choice(tmp_path, mutate, message):
    files = goal12(tmp_path)
    config = files['serving_config.json']
    mutate(config)
    goal12(tmp_path, **{'serving_config.json': config})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and message in stderr
    config['limits_reason'] = config['batch_by_eval']['swe']['batch_reason'] = 'measured: recorded cause'
    goal12(tmp_path, **{'serving_config.json': config})
    assert check(tmp_path, 'agentic-evals', evidence())[0] == 1


def test_benchmark_server_records_prefix_caching_instead_of_rejecting_it():
    server = dict(model='org/model', base_url='http://unused', max_num_seqs=32, prefix_caching=True,
                  implementation='models/autoports/model', generator_module='models.autoports.model.generator',
                  model_revision='1', tokenizer_revision='1', precision='p', layer_count=2,
                  configured_layer_count=2, source_commits={'tt-metal': '1'}, hardware='hw',
                  max_model_len=8192, server_command=['vllm', 'serve', '--max-num-seqs', '32'])
    validate_server(server, 32, 'org/model', 'http://unused')
    del server['prefix_caching']
    with pytest.raises(ValueError, match='record whether prefix caching'):
        validate_server(server, 32, 'org/model', 'http://unused')
