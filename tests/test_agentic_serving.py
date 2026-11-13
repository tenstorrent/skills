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
                    'trace_captures_after_warmup': {'captures': 0, 'retirements': 0},
                    'growing_contexts': {'concurrency': 16, 'final_context_tokens': 20000,
                                         'late_hit_fraction': 0.93, 'preemptions': 0},
                    'prefill_by_length': {'4096': {'prefill_tok_s': 1}},
                    'host_contention': {'idle': {'tok_s': 1}, 'loaded': {'tok_s': 1}}},
    }


def check(model, stage, data, inputs=None):
    """Run the gate from a target checkout at `model`, with `inputs` as the operator's inputs.json."""
    (model / 'doc').mkdir(parents=True, exist_ok=True)
    (model / 'doc/agentic_serving.json').write_text(json.dumps(data))
    if inputs is not None:
        (model / 'bringup/agentic_evals').mkdir(parents=True, exist_ok=True)
        (model / 'bringup/agentic_evals/inputs.json').write_text(json.dumps(inputs))
    result = subprocess.run([sys.executable, str(CHECKER), '--model-dir', str(model), '--stage', stage],
                            text=True, capture_output=True, cwd=model)
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
    ('vllm', lambda e: e['serving']['growing_contexts'].update(late_hit_fraction=0.4),
     ('serving', 'growing_contexts', 'reason')),
    ('vllm', lambda e: e['serving']['growing_contexts'].update(final_context_tokens=4000),
     ('serving', 'growing_contexts', 'reason')),
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
          'cache_pressure', 'cache_bytes', 'memory_headroom', 'cutoffs', 'eval_validity', 'accuracy_drift',
          'generation_share', 'prefill_under_load', 'host_contention')
CALCULATIONS = ('batch', 'agents', 'run_hours', 'task_lists', 'agent_timeout', 'output_cap', 'bytes', 'cache_agents',
                'block_cost', 'prefill_padding')
PREFLIGHT = ('release_values', 'registry', 'disk', 'admission', 'release_route', 'smoke')



BATCHES = [1, 8, 16, 32]
LIMITS = {'agent_timeout_s': 21600, 'request_timeout_s': 3600, 'output_cap_tokens': 32768}


def rates(lengths=(4096, 20000, 32768)):
    rows = [{'batch': b, 'input_tokens': n, 'prefill_tok_s': 900.0, 'decode_tok_s_user': 20.0, 'ttft_s': 4.0,
             'step_ms': 50.0, 'predicted': {'decode_tok_s_user_low': 18.0}} for b in BATCHES for n in lengths]
    return {'server': {'max_num_seqs': 32}, 'validated_batches': BATCHES, 'rows': rows,
            'release_rows': [{'isl': 4096, 'osl': 128, 'concurrency': 8, 'route': 'eval', 'decode_tok_s_user': 19.5}],
            'release_source': 'tt-inference-server model spec (closest model)'}


def scored_run(run_id='gpqa10', phase='phase1', suite='r1_gpqa_diamond', solved=7, n=10):
    failures = [{'task': f'{suite}-{i}', 'cause': 'wrong', 'evidence': 'final answer B, key C'}
                for i in range(n - solved)]
    return {'id': run_id, 'phase': phase, 'suite': suite,
            'tasks': {'rule': 'stratified by domain, fixed list', 'names': [f'{suite}-{i}' for i in range(n)]},
            'config': {'pool_tokens': 400000, 'agents': 8, 'batch': 8, 'max_num_seqs': 8, 'sha': 'abc123',
                       'precision': 'tt/selected_precision_config.json',
                       'server_size': {'free_gb': 26.0, 'pool_gb': 20.0, 'working_set_tokens': 40000},
                       'network': 'per task allow_internet', 'reasoning': {'thinking': True, 'effort': 'high'},
                       'limits': {**LIMITS, 'reference': dict(LIMITS), 'source': 'tt-inference-server eval config'}},
            'score': {'solved': solved, 'n': n, 'interval_95': [round(v, 1) for v in wilson(solved, n)]},
            'reference': {'value': 84.3, 'source': 'model card', 'reasoning': 'thinking on'}, 'band_pass': True,
            'predicted_hours': 2.0, 'explained_hours': 1.8, 'measured_hours': 2.0, 'collected': True,
            'failures': failures}


def wilson(solved, n, z=1.96):
    p = solved / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / d
    return 100 * (c - h), 100 * (c + h)


def write_digest(model, run, flagged=(), unmapped=()):
    """reply_digest.py output for `run`: one digest row per listed task, `flagged` tasks with a degenerate reply."""
    folder = model / 'doc/agentic_evals/current' / run['id'] / 'digest'
    folder.mkdir(parents=True, exist_ok=True)
    names = list((run.get('tasks') or {}).get('names') or []) + [row['task'] for row in run.get('failures') or []]
    tasks = {}
    for name in dict.fromkeys(names):
        tasks[name] = {'task': name, 'calls': 3, 'capped': int(name in flagged), 'flagged': int(name in flagged),
                       'digest': f'{name}.json'}
        (folder / f'{name}.json').write_text(json.dumps({'task': name, 'calls': []}))
    (folder / 'summary.json').write_text(json.dumps({'run': run['id'], 'tasks': tasks, 'unmapped': {
        'replies': len(unmapped), 'capped_or_flagged': [{'id': i, 'capped': True} for i in unmapped]}}))
    return folder


def goal12(model, runs=None, phases=None, inputs=None, data=None, extra=None, digests=True, logging=True):
    """Write a passing quick-mode goal-12 evidence set at `model` and run the gate; returns (code, stderr)."""
    runs = runs if runs is not None else [scored_run()]
    evals = model / 'doc/agentic_evals'
    evals.mkdir(parents=True, exist_ok=True)
    if logging:
        (evals / 'logging_check.json').write_text(json.dumps({'pass': True, 'id': 'chatcmpl-1', 'tokens': 12,
                                                              'prompt_tokens': 20, 'finish': 'stop'}))
    if digests:
        for run in runs:
            if not (evals / 'current' / run['id'] / 'digest/summary.json').is_file():
                write_digest(model, run)
    (model / 'doc/benchmark').mkdir(parents=True, exist_ok=True)
    (model / 'doc/benchmark/serving_rates.json').write_text(json.dumps(rates()))
    results = {'runs': runs, 'phases': phases if phases is not None else {'phase1': {'run': 'gpqa10', 'pass': True}}}
    results.update(extra or {})
    (evals / 'results.json').write_text(json.dumps(results))
    (evals / 'runs.json').write_text(json.dumps({'runs': [{'id': r['id'], 'conclusion': 'collected'} for r in runs
                                                          if r.get('collected')]}))
    (evals / 'learned.json').write_text(json.dumps({'best_config': {}, 'harness_facts': [], 'generic_fixes': []}))
    (evals / 'REPORT.md').write_text('# Phases\n')
    return check(model, 'agentic-evals', data if data is not None else evidence(),
                 inputs if inputs is not None else {'mode': 'quick'})


def test_quick_run_passes(tmp_path):
    assert goal12(tmp_path) == (0, '')


def drop_failure_row(run):
    run['failures'].pop()


def raise_agent_limit(run):
    run['config']['limits']['agent_timeout_s'] = 36000


def drop_reference(run):
    del run['reference']


def explain_badly(run):
    run.update(measured_hours=3.0, explained_hours=1.8)


def uncollect(run):
    run['collected'] = False


def drop_reasoning(run):
    del run['config']['reasoning']


def cap_without_rerun(run):
    run['failures'][0]['cause'] = 'cutoff_progressing'


@pytest.mark.parametrize('fault, message', [
    (drop_failure_row, 'every failed task needs one'),
    (drop_reference, 'no reference value'),
    (uncollect, 'not collected'),
    (drop_reasoning, 'reasoning setting'),
    (cap_without_rerun, 'at a higher limit'),
])
def test_quick_run_fault_is_critical(tmp_path, fault, message):
    run = scored_run()
    fault(run)
    code, stderr = goal12(tmp_path, runs=[run], phases={'phase1': {'run': 'gpqa10', 'pass': False}})
    assert code == 2 and message in stderr


def test_operator_decision_covers_a_missing_reference(tmp_path):
    run = scored_run()
    drop_reference(run)
    inputs = {'mode': 'quick', 'decisions': [{'accept': 'phase1', 'reason': 'operator: no reference yet'}]}
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'no reference'}}
    code, stderr = goal12(tmp_path, runs=[run], phases=phases, inputs=inputs)
    assert code == 1 and 'accepted by the operator' in stderr


def test_uncollected_run_is_critical_even_with_a_decision(tmp_path):
    run = scored_run()
    uncollect(run)
    inputs = {'mode': 'quick', 'decisions': [{'accept': 'phase1', 'reason': 'operator'}]}
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'not collected'}}
    assert goal12(tmp_path, runs=[run], phases=phases, inputs=inputs)[0] == 2


def test_decision_does_not_excuse_a_rule_break(tmp_path):
    run = scored_run()
    cap_without_rerun(run)
    inputs = {'mode': 'quick', 'decisions': [{'accept': 'phase1', 'reason': 'operator'}]}
    assert goal12(tmp_path, runs=[run], inputs=inputs)[0] == 2


@pytest.mark.parametrize('solved, n, fault, message', [
    (4, 10, None, 'against reference 84.3'),
    (7, 10, explain_badly, 'explain'),
    (5, 6, None, 'scored 6 of 10 tasks'),
])
def test_phase_not_passed_yet_is_advisory_with_a_reason(tmp_path, solved, n, fault, message):
    run = scored_run(solved=solved, n=n)
    low, high = wilson(solved, n)
    run['band_pass'] = low <= 84.3 <= high or 100 * solved / n >= 84.3
    if fault:
        fault(run)
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'phase time ended'}}
    extra = {'anomalies': [fix_attempt()]}
    code, stderr = goal12(tmp_path, runs=[run], phases=phases, extra=extra)
    assert code == 1 and message in stderr and 'not passed yet' in stderr
    del phases['phase1']['reason']
    code, stderr = goal12(tmp_path, runs=[run], phases=phases, extra=extra)
    assert code == 2 and 'must say why' in stderr


def test_limit_failure_with_settings_tried_passes(tmp_path):
    run = scored_run()
    run['failures'][0].update(cause='loop_cap', rerun={'solved': False, 'tried': [
        {'reasoning': {'thinking': True, 'effort': 'high'}, 'result': 'loop to the cap again'},
        {'reasoning': {'thinking': False}, 'result': 'wrong answer in 2,100 tokens'}]})
    assert goal12(tmp_path, runs=[run]) == (0, '')
    run['failures'][0]['rerun']['tried'] = [{'limits': {**LIMITS, 'seed': 2}, 'result': 'loop again'}]
    assert goal12(tmp_path, runs=[run]) == (0, ''), 'a tried row needs no reasoning setting'
    run['failures'][0]['rerun']['tried'] = [{'result': 'no setting recorded'}]
    assert goal12(tmp_path, runs=[run])[0] == 2
    del run['failures'][0]['rerun']
    assert goal12(tmp_path, runs=[run]) == (0, ''), 'reasoning probes are optional'


def test_wave_of_five_tasks_recorded_as_one_run_passes(tmp_path):
    run = scored_run(solved=5)
    wave = {'setting': 'output cap 2x', 'result': 'wave of 5 tasks, one harness run, 2 solved'}
    for row in run['failures']:
        row.update(cause='loop_cap', rerun={'tried': [wave]})
    assert len(run['failures']) == 5
    run['band_pass'] = False
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'phase time ended'}}
    code, stderr = goal12(tmp_path, runs=[run], phases=phases, extra={'anomalies': [fix_attempt()]})
    assert code == 1 and 'CRITICAL' not in stderr


def test_list_run_above_the_reference_limits_is_advisory(tmp_path):
    run = scored_run()
    raise_agent_limit(run)
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 1 and 'single-task reruns' in stderr


def test_cutoff_while_progressing_needs_a_higher_limit_rerun(tmp_path):
    run = scored_run()
    probe = {'reasoning': {'thinking': False}, 'result': 'cut off at the cap again, still progressing',
             'limits': dict(LIMITS)}
    run['failures'][0].update(cause='cutoff_progressing', rerun={'tried': [probe]})
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'higher limit' in stderr
    raised = {'reasoning': {'thinking': True, 'effort': 'high'}, 'result': 'solved',
              'limits': {**LIMITS, 'output_cap_tokens': 2 * LIMITS['output_cap_tokens']}}
    run['failures'][0]['rerun'] = {'tried': [probe, raised], 'counted': True}
    run['score'] = {'solved': 8, 'n': 10, 'interval_95': [round(v, 1) for v in wilson(8, 10)]}
    assert goal12(tmp_path, runs=[run]) == (0, '')


def test_recorded_pass_must_agree_with_the_gate(tmp_path):
    run = scored_run(solved=4)
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'band_pass is True' in stderr


def test_interval_must_be_wilson(tmp_path):
    run = scored_run()
    run['score']['interval_95'] = [55.0, 85.0]
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'Wilson' in stderr


def test_a_voided_list_is_critical(tmp_path):
    run = scored_run()
    run['voided'] = True
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'never the list' in stderr


def test_rerun_counts_only_for_infrastructure_or_a_higher_limit(tmp_path):
    run = scored_run(solved=8)
    run['failures'].append({'task': 'x', 'cause': 'wrong', 'evidence': 'e', 'rerun': {'solved': True, 'counted': True}})
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'neither infrastructure' in stderr
    run['failures'][-1]['cause'] = 'infrastructure'
    assert goal12(tmp_path, runs=[run]) == (0, '')


def test_agents_above_max_num_seqs_and_batch_choice(tmp_path):
    run = scored_run()
    run['config']['batch'] = 32
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 1 and 'smallest validated batch' in stderr
    run['config'].update(agents=16, batch=16)
    assert goal12(tmp_path, runs=[run])[0] == 2


def test_server_size_must_be_recorded(tmp_path):
    run = scored_run()
    del run['config']['server_size']
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'measured at launch' in stderr


def qwen_quick_run():
    """3 agents on a one-request pool with 26 GB free per device."""
    run = scored_run()
    run['config'].update(pool_tokens=32768, agents=3, batch=8)
    run['config']['server_size'].update(pool_gb=1.5, working_set_tokens=32768)
    return run


def test_pool_far_below_free_memory_is_critical_without_a_reason(tmp_path):
    run = qwen_quick_run()
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'below 50% of the 26.0 GB' in stderr and 'exceed the 32768-token pool' in stderr
    run['config']['server_size']['reason'] = 'measured: trace region needs the rest'
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 1 and 'trace region' in stderr


def test_pool_at_half_of_free_memory_passes_and_agents_must_fit_it(tmp_path):
    run = scored_run()
    run['config']['server_size']['pool_gb'] = 13.0
    assert goal12(tmp_path, runs=[run]) == (0, '')
    run['config']['server_size']['pool_gb'] = 12.9
    assert goal12(tmp_path, runs=[run])[0] == 2
    run = scored_run()
    run['config']['server_size']['working_set_tokens'] = 60000
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and '8 agents x 60000' in stderr


def anomaly_run(run_id='gpqa10'):
    """GPQA task 102: correct reasoning, then stray apostrophes and a 5-character stream to the cap."""
    run = scored_run(run_id)
    run['failures'][0].update(cause='anomaly', evidence='from char 14,000: 5 distinct chars in the last 4,096')
    return run


def fix_attempt(**extra):
    return {'phase': 'phase1', 'tasks': ['r1_gpqa_diamond-0'], 'test': 'run_autoregressive --prompt-file ...',
            'prompt': 'doc/agentic_evals/anomalies/gpqa102_prefix.txt', 'seed': 0,
            'served': {'precision': 'tt/selected_precision_config.json', 'result': '9/10 continuations degenerate'},
            'full_precision': {'result': '0/10 degenerate', 'pass': True},
            'changed': 'head and MLP down to BF8 HiFi2', **extra}


def test_anomaly_needs_a_reproducible_test_with_a_full_precision_result(tmp_path):
    run = anomaly_run()
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'has no anomalies entry' in stderr
    code, stderr = goal12(tmp_path, runs=[run], extra={'anomalies': [fix_attempt(full_precision={'result': 'not run'})]})
    assert code == 2 and 'full_precision (result, pass)' in stderr
    assert goal12(tmp_path, runs=[run], extra={'anomalies': [fix_attempt()]}) == (0, '')


def test_phase_below_its_band_needs_a_fix_attempt(tmp_path):
    run = scored_run(solved=4)
    run['band_pass'] = False
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'below band'}}
    code, stderr = goal12(tmp_path, runs=[run], phases=phases)
    assert code == 2 and 'no anomalies entry (fix attempt)' in stderr
    code, stderr = goal12(tmp_path, runs=[run], phases=phases, extra={'anomalies': [fix_attempt()]})
    assert code == 1 and 'not passed yet' in stderr


def test_precision_fix_takes_the_fastest_passing_config_and_reruns_the_list(tmp_path):
    first = anomaly_run('gpqa10-bfp4')
    tried = [{'config': 'bf8-head', 'pass': False, 'decode_tok_s_user': 21.0},
             {'config': 'bf8-head-down', 'pass': True, 'decode_tok_s_user': 19.0},
             {'config': 'bf16-full', 'pass': True, 'decode_tok_s_user': 12.0}]
    entry = fix_attempt(configs_tried=tried, chosen='bf16-full', remeasured_rows=[{'batch': 8, 'input_tokens': 20000}])
    rerun = scored_run(solved=8)
    rerun['config']['precision'] = 'bf8-head-down'
    code, stderr = goal12(tmp_path, runs=[first, rerun], extra={'anomalies': [entry]})
    assert code == 2 and "fastest passing config tried is 'bf8-head-down'" in stderr
    entry['chosen'] = 'bf8-head-down'
    assert goal12(tmp_path, runs=[first, rerun], extra={'anomalies': [entry]}) == (0, '')
    for key, value, message in (('configs_tried', [{'config': 'bf8-head-down', 'pass': True}], 'measured decode'),
                                ('remeasured_rows', [], 'remeasured_rows')):
        code, stderr = goal12(tmp_path, runs=[first, rerun], extra={'anomalies': [{**entry, key: value}]})
        assert code == 2 and message in stderr
    rerun['config']['precision'] = 'tt/selected_precision_config.json'
    code, stderr = goal12(tmp_path, runs=[first, rerun], extra={'anomalies': [entry]})
    assert code == 2 and 'rerun the list' in stderr


def test_agentic_mode_needs_phase_2(tmp_path):
    code, stderr = goal12(tmp_path, inputs={'mode': 'agentic'})
    assert code == 2 and 'phase2 has no scored run' in stderr
    runs = [scored_run(), scored_run('swe10', 'phase2', 'swe_bench_verified', solved=6)]
    for run in runs[1:]:
        run['reference'] = {'value': 61.2, 'source': 'operator floor', 'reasoning': 'unknown'}
    phases = {'phase1': {'run': 'gpqa10', 'pass': True}, 'phase2': {'run': 'swe10', 'pass': True}}
    assert goal12(tmp_path, runs=runs, phases=phases, inputs={'mode': 'agentic'}) == (0, '')


def test_extended_mode_needs_estimates_and_fitting_runs(tmp_path):
    runs = [scored_run(), scored_run('swe10', 'phase2', 'swe_bench_verified', solved=6),
            scored_run('gpqa80', 'extended', 'r1_gpqa_diamond', solved=66, n=80),
            scored_run('swe40', 'extended', 'swe_bench_verified', solved=24, n=40)]
    for run in runs:
        if run['suite'] == 'swe_bench_verified':
            run['reference'] = {'value': 61.2, 'source': 'operator floor', 'reasoning': 'unknown'}
    phases = {'phase1': {'run': 'gpqa10', 'pass': True}, 'phase2': {'run': 'swe10', 'pass': True}}
    estimates = {'estimates': {'r1_gpqa_diamond': 18.0, 'swe_bench_verified': 30.0}}
    assert goal12(tmp_path, runs=runs, phases=phases, inputs={'mode': 'extended'}, extra=estimates) == (0, '')
    runs[2]['predicted_hours'] = 14.0
    code, stderr = goal12(tmp_path, runs=runs, phases=phases, inputs={'mode': 'extended'}, extra=estimates)
    assert code == 2 and 'run_timeout_hours' in stderr


def test_phase_1_not_passed_still_lets_phase_2_count_and_skips_extended(tmp_path):
    runs = [scored_run(solved=4), scored_run('swe10', 'phase2', 'swe_bench_verified', solved=6)]
    runs[0]['band_pass'] = False
    runs[1]['reference'] = {'value': 61.2, 'source': 'operator floor', 'reasoning': 'unknown'}
    phases = {'phase1': {'run': 'gpqa10', 'pass': False, 'reason': 'below band at hour 12'},
              'phase2': {'run': 'swe10', 'pass': True, 'start_hour': 12}}
    extra = {'anomalies': [fix_attempt()]}
    assert goal12(tmp_path, runs=runs, phases=phases, inputs={'mode': 'agentic'}, extra=extra)[0] == 1
    code, stderr = goal12(tmp_path, runs=runs, phases=phases, inputs={'mode': 'extended'},
                          extra={**extra, 'estimates': {'r1_gpqa_diamond': 18.0, 'swe_bench_verified': 30.0}})
    assert code == 1 and 'starts after both phases pass' in stderr


def test_existing_entry_skips_the_serving_evidence(tmp_path):
    code, stderr = goal12(tmp_path, inputs={'mode': 'quick', 'entry': 'existing'}, data={})
    assert (code, stderr) == (0, '')


def test_unknown_mode_is_critical(tmp_path):
    assert goal12(tmp_path, inputs={'mode': 'light'})[0] == 2


def test_stage_12_needs_stage_11_rates(tmp_path):
    goal12(tmp_path)
    (tmp_path / 'doc/benchmark/serving_rates.json').unlink()
    code, stderr = check(tmp_path, 'agentic-evals', evidence(), {'mode': 'quick'})
    assert code == 2 and 'serving_rates.json' in stderr


def anomaly_record(anomaly=False):
    return {'replies': [{'prompt': 'p1.txt', 'seed': 1, 'output_tokens': 4000, 'distinct_tail_chars': 60,
                         'repeated_ngram': False, 'anomaly': anomaly}], 'anomalies': int(anomaly)}


def benchmark(model, data, record=anomaly_record()):
    (model / 'doc/benchmark').mkdir(parents=True, exist_ok=True)
    (model / 'doc/benchmark/serving_rates.json').write_text(json.dumps(data))
    if record is not None:
        (model / 'doc/benchmark/anomaly_check.json').write_text(json.dumps(record))
    return check(model, 'benchmark', evidence(), {})


def test_stage_11_needs_the_anomaly_record(tmp_path):
    code, stderr = benchmark(tmp_path, rates(), record=None)
    assert code == 2 and 'anomaly_check.json' in stderr


def test_stage_11_anomaly_record_without_replies_is_critical(tmp_path):
    assert benchmark(tmp_path, rates(), record={'replies': []})[0] == 2


def test_stage_11_anomaly_found_is_advisory(tmp_path):
    code, stderr = benchmark(tmp_path, rates(), record=anomaly_record(True))
    assert code == 1 and 'anomaly' in stderr


def test_stage_11_grid_passes(tmp_path):
    assert benchmark(tmp_path, rates()) == (0, '')


def test_stage_11_needs_long_context_and_release_rows(tmp_path):
    code, stderr = benchmark(tmp_path, rates(lengths=(4096, 20000)))
    assert code == 2 and 'long-context' in stderr
    data = rates()
    data['release_rows'][0]['route'] = 'local'
    code, stderr = benchmark(tmp_path, data)
    assert code == 2 and 'eval route' in stderr


def test_stage_11_row_left_by_the_budget_is_advisory_when_listed(tmp_path):
    data = rates()
    data['rows'] = [row for row in data['rows'] if not (row['batch'] == BATCHES[0] and row['input_tokens'] == 32768)]
    assert benchmark(tmp_path, data)[0] == 2
    data['missing_rows'] = [{'batch': BATCHES[0], 'input_tokens': 32768}]
    assert benchmark(tmp_path, data)[0] == 2
    data['missing_rows'][0]['reason'] = 'benchmark_budget_hours ended'
    code, stderr = benchmark(tmp_path, data)
    assert code == 1 and 'long-context' in stderr


def test_stage_11_row_below_prediction_needs_a_cause(tmp_path):
    data = rates()
    data['rows'][0]['decode_tok_s_user'] = 10.0
    assert benchmark(tmp_path, data)[0] == 2
    data['rows'][0]['cause'] = 'host contention during the long prefill'
    assert benchmark(tmp_path, data) == (0, '')


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




def test_fixed_pool_budget_is_a_shortfall(tmp_path):
    data = evidence()
    data['ledger']['pool_sizing'] = 'fixed'
    assert check(tmp_path, 'full-model', data)[0] == 2
    data['ledger']['pool_sizing_reason'] = 'measured: adapter API has no free-DRAM query'
    assert check(tmp_path, 'full-model', data)[0] == 1


REPLAY = PLUGIN / 'scripts/agent_replay.py'


def test_agent_replay_reports_hits_and_prefill_amplification(tmp_path):
    """A fake server caches each conversation's previous prompt; the replay must recover that."""
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'vllm:num_preemptions_total{engine="0"} 3\n'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert request['max_tokens'] == 1
            key = request['messages'][0]['content']
            prompt = sum(len(m['content']) for m in request['messages'])
            cached = seen.get(key, 0)
            seen[key] = prompt
            body = json.dumps({'usage': {'prompt_tokens': prompt, 'prompt_tokens_details': {'cached_tokens': cached}}})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    histories = tmp_path / 'histories'
    histories.mkdir()
    for agent in ('a', 'b'):
        lines = []
        messages = [{'role': 'system', 'content': agent * 100}]
        for turn in range(3):
            messages = messages + [{'role': 'user', 'content': 'x' * 10}]
            lines.append(json.dumps({'model': 'm', 'messages': messages, 'max_tokens': 4096}))
        (histories / f'{agent}.jsonl').write_text('\n'.join(lines))
    out = tmp_path / 'replay.json'
    result = subprocess.run([sys.executable, str(REPLAY), '--base-url', f'http://127.0.0.1:{server.server_port}/v1',
                             '--histories', str(histories), '--agents', '2', '--out', str(out)],
                            text=True, capture_output=True)
    server.shutdown()
    assert result.returncode == 0, result.stderr
    summary = json.loads(out.read_text())
    assert summary['calls_done'] == 6 and summary['histories_finished'] == 2 and summary['errors'] == 0
    # per agent prompts 110, 120, 130 with cached 0, 110, 120
    assert summary['prompt_tokens'] == 720 and summary['cached_tokens'] == 460
    assert summary['prefilled_tokens'] == 260 and summary['final_context_tokens'] == 260
    assert summary['prefilled_per_final_context'] == 1.0
    assert summary['preemptions'] == 0



@pytest.mark.parametrize('stage, key', [
    ('vllm', 'growing_contexts'),
    ('optimized-vllm', 'growing_contexts'),
    ('optimized-vllm', 'prefill_by_length'),
    ('optimized-vllm', 'host_contention'),
])
def test_serving_stages_require_their_agent_load_measurements(tmp_path, stage, key):
    data = evidence()
    del data['serving'][key]
    code, stderr = check(tmp_path, stage, data)
    assert code == 2 and key in stderr


def test_vllm_stage_does_not_require_the_stage_10_measurements(tmp_path):
    data = evidence()
    del data['serving']['prefill_by_length'], data['serving']['host_contention']
    assert check(tmp_path, 'vllm', data) == (0, '')


def test_growing_contexts_without_prefix_caching_records_load_only(tmp_path):
    data = evidence()
    data['serving'].update(prefix_caching=False, reason='recurrent_state: state not recoverable from KV blocks')
    del data['serving']['growing_contexts']['late_hit_fraction']
    code, stderr = check(tmp_path, 'vllm', data)
    assert code == 1 and 'recurrent_state' in stderr and 'growing_contexts' not in stderr


def test_logging_check_is_required_before_any_eval_run(tmp_path):
    code, stderr = goal12(tmp_path, logging=False)
    assert code == 2 and 'logging_check.json' in stderr
    (tmp_path / 'doc/agentic_evals/logging_check.json').write_text(json.dumps({'pass': False, 'problems': ['no usage']}))
    code, stderr = goal12(tmp_path, logging=False)
    assert code == 2 and 'did not pass' in stderr


def test_every_run_needs_a_reply_digest(tmp_path):
    code, stderr = goal12(tmp_path, digests=False)
    assert code == 2 and 'no reply digest' in stderr


def test_failed_task_without_a_digest_row_is_critical(tmp_path):
    run = scored_run()
    folder = write_digest(tmp_path, run)
    summary = json.loads((folder / 'summary.json').read_text())
    del summary['tasks'][run['failures'][0]['task']]
    (folder / 'summary.json').write_text(json.dumps(summary))
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'has no reply digest row' in stderr


def test_failed_task_with_no_mapped_calls_is_critical_unless_infrastructure(tmp_path):
    run = scored_run()
    folder = write_digest(tmp_path, run)
    summary = json.loads((folder / 'summary.json').read_text())
    summary['tasks'][run['failures'][0]['task']]['calls'] = 0
    (folder / 'summary.json').write_text(json.dumps(summary))
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'no model calls mapped' in stderr
    run['failures'][0].update(cause='infrastructure', evidence='image pull failed')
    assert goal12(tmp_path, runs=[run]) == (0, '')


def test_capped_reply_mapped_to_no_task_is_critical_without_a_reason(tmp_path):
    run = scored_run()
    write_digest(tmp_path, run, unmapped=['chatcmpl-dropped'])
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'map to no task' in stderr
    run['unmapped_reason'] = 'replies of the stage-11 smoke run on the same server'
    assert goal12(tmp_path, runs=[run]) == (0, '')


def test_flagged_reply_needs_cause_anomaly_or_a_reason(tmp_path):
    run = scored_run()
    task = run['failures'][0]['task']
    write_digest(tmp_path, run, flagged=[task])
    code, stderr = goal12(tmp_path, runs=[run])
    assert code == 2 and 'flagged (degenerate)' in stderr
    run['flag_reasons'] = {task: 'triage: tail is a long base64 blob the task printed, not a token loop'}
    assert goal12(tmp_path, runs=[run]) == (0, '')


def test_flagged_reply_with_cause_anomaly_passes_the_digest_check(tmp_path):
    run = anomaly_run()
    write_digest(tmp_path, run, flagged=[run['failures'][0]['task']])
    code, stderr = goal12(tmp_path, runs=[run], extra={'anomalies': [fix_attempt()]})
    assert 'flagged' not in stderr and 'digest' not in stderr


def test_unexplained_time_with_a_reason_is_advisory_and_without_one_critical(tmp_path):
    run = scored_run()
    del run['explained_hours']
    code, stderr = goal12(tmp_path, runs=[run], phases={'phase1': {'run': 'gpqa10', 'pass': False,
                                                                     'reason': 'time not explained'}})
    assert code == 2 and 'explain_hours.py' in stderr
    run['explained_reason'] = 'per-call accounting lost: response logging was off'
    code, stderr = goal12(tmp_path, runs=[run], phases={'phase1': {'run': 'gpqa10', 'pass': False,
                                                                     'reason': 'time not explained'}})
    assert code == 1 and 'response logging was off' in stderr


def test_explain_tolerance_is_relative_to_the_explained_time(tmp_path):
    run = scored_run()
    run.update(measured_hours=1.0, explained_hours=1.3)       # 23% of the explained time, 30% of the measured
    assert goal12(tmp_path, runs=[run]) == (0, '')
    run.update(measured_hours=1.0, explained_hours=0.78)      # 28% of the explained time, 22% of the measured
    code, stderr = goal12(tmp_path, runs=[run], phases={'phase1': {'run': 'gpqa10', 'pass': False,
                                                                     'reason': 'time 28% off'}})
    assert code == 1 and 'not passed yet' in stderr


def test_stage_11_gate_reads_the_lengths_it_recorded_not_the_live_workload(tmp_path):
    """Goal 12 rewrites the live workload; stage 11's verdict must not change after it."""
    data = rates()
    data['workload'] = {'typical_context_tokens': 20000, 'working_set_tokens': 32768}
    (tmp_path / 'doc/benchmark').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'doc/benchmark/anomaly_check.json').write_text(json.dumps(anomaly_record()))
    (tmp_path / 'doc/benchmark/serving_rates.json').write_text(json.dumps(data))
    moved = evidence()
    moved['workload']['working_set_tokens'] = 35809           # what goal 12 measured later
    assert check(tmp_path, 'benchmark', moved, {}) == (0, '')
