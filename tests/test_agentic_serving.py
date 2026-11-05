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


def goal12(model, **overrides):
    """Write a complete, passing goal-12 evidence set; overrides replace whole files."""
    evals = model / 'doc/agentic_evals'
    (evals / 'release/swe').mkdir(parents=True, exist_ok=True)
    files = {
        'plan.json': {'mode': 'light', 'schedule': [{'step': 'release', 'start_hours': 20}],
                      'release': {'dry_run': 'runs/release-dry-run', 'projected_hours': 9.5}},
        'REPORT.md': '# Report',
        'estimate.json': {'evals': {'swe': {'decision': 'go', 'reason': 'fits'}}},
        'release/swe/results.json': {'actual_hours': 6.5},
        'survey.json': {'checks': {k: {'value': 1, 'healthy': True} for k in CHECKS},
                        'calculations': {k: 1 for k in CALCULATIONS}, 'shortfalls': []},
        'preflight.json': {k: {'ok': True} for k in PREFLIGHT},
        'cohorts.json': {'swe': {'source': 'default_rule', 'rule': 'hash order within repositories, round-robin',
                                 'rungs': {'smoke': ['astropy__astropy-14508', 'django__django-14034']}}},
        'learned.json': {'harness_facts': [], 'suite_timings': [], 'generic_fixes': []},
        'efforts.json': {
            'efforts': [
                {'problem': 'padded prefill', 'owner': 'prefill', 'status': 'done',
                 'goal': 'prefill one live row at batch-1 cost', 'plan': 'runs/prefill/plan.md',
                 'predicted': {'saving_hours': {'swe': 3.0, 'terminal': 1.5}, 'device_hours': 2, 'basis': 'calculated'},
                 'confirm': {'method': 'prefill vs batch shape', 'success': 'one live row costs batch-1 time'},
                 'box_hours': 4, 'used_device_hours': 3, 'change': 'kept', 'problem_status': 'solved',
                 'confirmed': 'prefill 0.9 s per live row', 'evidence': 'runs/prefill'},
                {'problem': 'block size', 'status': 'not_run', 'reason': 'below the budget line',
                 'predicted': {'saving_hours': {'swe': 0.5}, 'device_hours': 3, 'basis': 'guess'},
                 'confirm': {'method': 'cache replay', 'success': 'hit rate above 0.9'}},
            ],
            'rankings': [{'after': 'survey', 'order': [
                {'problem': 'padded prefill', 'saving_hours': 4.5, 'device_hours': 2},
                {'problem': 'block size', 'saving_hours': 0.5, 'device_hours': 3}]}],
        },
        'serving_config.json': {'agent_timeout_s': 6300, 'request_timeout_s': 3600, 'output_cap_tokens': 81920,
                                'release_limits': {'agent_timeout_s': 6300, 'request_timeout_s': 3600,
                                                   'output_cap_tokens': 81920},
                                'validated_batches': [1, 8, 16, 32], 'max_num_seqs': 32,
                                'batch_by_eval': {'swe': {'batch': 32, 'planned_clients': 29}}},
    }
    files.update(overrides)
    for name, body in files.items():
        if body is None:
            (evals / name).unlink(missing_ok=True)
        else:
            (evals / name).write_text(body if isinstance(body, str) else json.dumps(body))
    return files


def test_agentic_evals_requires_decisions_and_results(tmp_path):
    goal12(tmp_path, **{'release/swe/results.json': None})
    assert check(tmp_path, 'agentic-evals', evidence())[0] == 2
    goal12(tmp_path, **{'estimate.json': {'evals': {
        'swe': {'decision': 'go', 'reason': 'fits'}, 'terminal': {'decision': 'stop', 'reason': 'latest start passed'}}}})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 1 and 'latest start passed' in stderr


def test_complete_goal12_evidence_passes(tmp_path):
    goal12(tmp_path)
    assert check(tmp_path, 'agentic-evals', evidence()) == (0, '')


@pytest.mark.parametrize('name', ['survey.json', 'preflight.json', 'efforts.json', 'cohorts.json', 'learned.json'])
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
    assert code == 2 and 'priced in hours' in stderr
    survey = files['survey.json']
    survey['shortfalls'] = [{'name': 'attention held twice', 'hours': 1.5}]
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
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].update(used_device_hours=9))
    assert code == 1 and '4 h box' in stderr


def test_effort_over_its_box_with_a_re_estimate_passes(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: e['efforts'][0].update(used_device_hours=9, re_estimate='2 h left'))
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
    (lambda c: c['batch_by_eval']['swe'].update(batch=8), 'the rule picks batch 32'),
    (lambda c: c['batch_by_eval']['swe'].update(planned_clients=8), 'the rule picks batch 8'),
    (lambda c: c.update(max_num_seqs=8), 'against max_num_seqs 8'),
])
def test_release_limits_and_batch_choice(tmp_path, mutate, message):
    files = goal12(tmp_path)
    config = files['serving_config.json']
    mutate(config)
    goal12(tmp_path, **{'serving_config.json': config})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and message in stderr
    config['limits_reason'] = config['batch_by_eval']['swe']['batch_reason'] = 'measured: recorded cause'
    config['batch_by_eval']['swe']['agents_reason'] = 'measured: recorded cause'
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


def test_stop_with_a_banked_cohort_passes_and_without_one_is_advisory(tmp_path):
    estimate = {'evals': {'swe': {'decision': 'go', 'reason': 'fits'},
                          'terminal': {'decision': 'stop', 'reason': 'rung 2 does not fit', 'functional_at': 32}}}
    goal12(tmp_path, **{'estimate.json': estimate})
    assert check(tmp_path, 'agentic-evals', evidence()) == (0, '')
    del estimate['evals']['terminal']['functional_at']
    goal12(tmp_path, **{'estimate.json': estimate})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 1 and 'no cohort completed' in stderr


@pytest.mark.parametrize('row', [
    {'source': 'default_rule', 'rungs': {'smoke': ['a']}},
    {'source': 'guess', 'rule': 'r', 'rungs': {'smoke': ['a']}},
    {'source': 'inputs', 'rule': 'r', 'rungs': {'smoke': []}},
])
def test_task_lists_need_source_rule_and_tasks(tmp_path, row):
    goal12(tmp_path, **{'cohorts.json': {'swe': row}})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and 'selection rule' in stderr


@pytest.mark.parametrize('plan', [{'mode': 'everything', 'schedule': [{}]}, {'mode': 'extended'}])
def test_plan_needs_a_mode_and_a_schedule(tmp_path, plan):
    goal12(tmp_path, **{'plan.json': plan})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and 'mode' in stderr


def test_learned_needs_its_lists(tmp_path):
    goal12(tmp_path, **{'learned.json': {'harness_facts': []}})
    code, stderr = check(tmp_path, 'agentic-evals', evidence())
    assert code == 2 and 'suite_timings' in stderr


def validity_ranking(ledger, unblocks_first):
    order = [{'problem': 'padded prefill', 'saving_hours': 4.5, 'device_hours': 2},
             {'problem': 'terminal verifier offline', 'saving_hours': 0, 'device_hours': 3, 'unblocks': 'terminal'}]
    ledger['rankings'][0]['order'] = order[::-1] if unblocks_first else order


def test_validity_problem_ranks_above_speed_without_a_reason(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: validity_ranking(e, True))
    assert code == 0, stderr


def test_speed_above_a_validity_problem_needs_a_reason(tmp_path):
    code, stderr = efforts_with(tmp_path, lambda e: validity_ranking(e, False))
    assert code == 2 and 'validity problem' in stderr


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


@pytest.mark.parametrize('mode, release, message', [
    ('light', {'projected_hours': 9.5}, 'dry_run'),
    ('light', {'dry_run': 'runs/d', 'projected_hours': 13}, 'smaller list'),
    ('extended', {'dry_run': 'runs/d', 'projected_hours': {'swe': 11, 'terminal': 14}}, 'terminal projected'),
])
def test_release_needs_a_dry_run_and_a_projection_that_fits(tmp_path, mode, release, message):
    plan = {'mode': mode, 'schedule': [{}], 'release': release}
    goal12(tmp_path, **{'plan.json': plan})
    code, stderr = check(tmp_path, 'agentic-evals', evidence(), inputs={'mode': mode})
    assert code == 2 and message in stderr


def test_extended_mode_projects_each_suite_against_run_timeout_hours(tmp_path):
    plan = {'mode': 'extended', 'schedule': [{}],
            'release': {'dry_run': 'runs/d', 'projected_hours': {'swe': 11.5, 'terminal': 9}}}
    goal12(tmp_path, **{'plan.json': plan})
    assert check(tmp_path, 'agentic-evals', evidence(), inputs={'mode': 'extended'}) == (0, '')


def test_no_release_needed_when_every_eval_stopped(tmp_path):
    goal12(tmp_path, **{'plan.json': {'mode': 'light', 'schedule': [{}]},
                        'estimate.json': {'evals': {'swe': {'decision': 'stop', 'reason': 'r', 'functional_at': 5}}}})
    assert check(tmp_path, 'agentic-evals', evidence()) == (0, '')


@pytest.mark.parametrize('inputs, passes', [
    ({'run_timeout_hours': 8}, False),
    ({'run_timeout_hours': 10}, True),
    ({}, True),
])
def test_release_limit_comes_from_the_operator_inputs(tmp_path, inputs, passes):
    """projected 9.5 h against run_timeout_hours from inputs.json, default 12."""
    goal12(tmp_path)
    code, stderr = check(tmp_path, 'agentic-evals', evidence(), inputs=inputs)
    assert (code == 0) == passes, stderr


def test_plan_mode_must_match_the_operator_inputs(tmp_path):
    goal12(tmp_path)
    code, stderr = check(tmp_path, 'agentic-evals', evidence(), inputs={'mode': 'extended'})
    assert code == 2 and "mode 'extended'" in stderr


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
