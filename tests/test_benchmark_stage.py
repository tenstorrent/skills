"""Offline invariants for subset comparability and bounded benchmark execution."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'plugins/tt-model-bringup/runtime'
sys.path.insert(0, str(RUNTIME))
from benchmark_stage.subsets import allocate, digest, indices
from benchmark_stage.run import command
from benchmark_stage.check import check
from benchmark_stage.evidence import PERFORMANCE_METRICS


def test_reasoning_profiles_preserve_common_questions_and_gpqa_documents():
    profiles = RUNTIME / 'benchmark_stage/profiles'
    generic = json.loads((profiles / 'ci-v1.json').read_text())
    full = json.loads((profiles / 'ci-v1-reasoning-full.json').read_text())
    subset = json.loads((profiles / 'ci-v1-reasoning.json').read_text())
    for reasoning, count in ((full, 198), (subset, 128)):
        assert reasoning['manifest_sha256'] == digest({
            key: value for key, value in reasoning.items() if key != 'manifest_sha256'})
        for name in generic['tasks'].keys() & reasoning['tasks'].keys():
            for field in ('indices', 'document_sha256', 'population_sha256', 'fewshot_sha256'):
                assert generic['tasks'][name][field] == reasoning['tasks'][name][field]
        gpqa = reasoning['tasks']['gpqa_diamond_cot_zeroshot']
        assert gpqa['population'] == 198
        assert len(set(gpqa['indices'])) == count
        assert len(gpqa['document_sha256']) == count
        assert reasoning['groups']['gpqa_diamond_cot_zeroshot']['sample_count'] == count
    full_gpqa = full['tasks']['gpqa_diamond_cot_zeroshot']
    subset_gpqa = subset['tasks']['gpqa_diamond_cot_zeroshot']
    assert full_gpqa['indices'] == list(range(198))
    assert subset['candidate_from_manifest_sha256'] == full['manifest_sha256']
    assert subset_gpqa['document_processing'] == full_gpqa['document_processing']
    assert subset_gpqa['population_sha256'] == full_gpqa['population_sha256']
    assert subset_gpqa['document_sha256'] == [
        full_gpqa['document_sha256'][i] for i in subset_gpqa['indices']]


def test_recipe_variant_preserves_questions_and_rejects_changed_population():
    from benchmark_stage.subsets import reuse_indices
    source = {'tasks': {'original': {'population_sha256': 'same-documents', 'indices': [2, 9, 12]}}}
    assert reuse_indices(source, 'same-documents', 3) == ([2, 9, 12], 'original')
    with pytest.raises(ValueError, match='population'):
        reuse_indices(source, 'different-documents', 3)
    with pytest.raises(ValueError, match='sample count'):
        reuse_indices(source, 'same-documents', 2)


def test_published_subject_macro_does_not_use_micro_average():
    from benchmark_stage.evidence import metric_score
    raw = {'groups': {'mmlu': {'exact_match,strict_match': 0.8}},
           'group_subtasks': {'mmlu': ['small', 'large']},
           'results': {'small': {'exact_match,strict_match': 0.2},
                       'large': {'exact_match,strict_match': 1.0}}}
    assert metric_score(raw, 'mmlu', 'exact_match,strict_match') == 80
    assert metric_score(raw, 'mmlu', 'subject_macro:exact_match,strict_match', expected_children=['small', 'large']) == 60
    del raw['results']['small']
    with pytest.raises(ValueError, match='missing/invalid'):
        metric_score(raw, 'mmlu', 'subject_macro:exact_match,strict_match', expected_children=['small', 'large'])


@pytest.mark.parametrize('children', [['large'], ['small', 'large', 'large'], ['small', 'alien']])
def test_subject_macro_rejects_membership_drift_in_report(tmp_path, children):
    from benchmark_stage.report import write_report
    manifest = {'manifest_sha256': 'frozen', 'groups': {'mmlu': {
        'tasks': ['small', 'large'], 'sample_count': 4, 'population': 100}}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'mmlu').mkdir()
    (tmp_path / 'mmlu/results.json').write_text(json.dumps({
        'group_subtasks': {'mmlu': children},
        'results': {name: {'acc,none': .8} for name in children}}))
    config = {'model': 'test', 'manifest': str(tmp_path / 'manifest.json'), 'tasks': ['mmlu'],
              'references': {'mmlu': {'subject_macro:acc,none': {'score': 80}}}}
    with pytest.raises(ValueError, match='membership'):
        write_report(tmp_path, config, {'status': 'completed'})


def test_subset_is_content_stable_and_prefix_expands():
    docs = [{'question': f'q{i}'} for i in range(100)]
    small = indices('math', docs, 12)
    larger = indices('math', docs, 24)
    assert set(small) <= set(larger)
    reversed_docs = list(reversed(docs))
    reversed_selection = indices('math', reversed_docs, 12)
    assert {digest(docs[i]) for i in small} == {digest(reversed_docs[i]) for i in reversed_selection}
    assert len(set(small)) == 12


def test_proportional_subject_allocation():
    assert allocate({'a': 100, 'b': 200, 'c': 700}, 100) == {'a': 10, 'b': 20, 'c': 70}
    allocation = allocate({'a': 1, 'b': 999}, 10)
    assert sum(allocation.values()) == 10
    assert allocation['a'] == 1
    with pytest.raises(ValueError):
        allocate({'a': 100, 'b': 200}, 1)


def test_timeout_terminates_client_and_preserves_log(tmp_path):
    log = tmp_path / 'command.log'
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        command([sys.executable, '-u', '-c', 'import time; print("started"); time.sleep(30)'], log, started + 0.3)
    assert time.monotonic() - started < 5
    assert 'started' in log.read_text()


def test_failed_client_is_not_success(tmp_path):
    with pytest.raises(RuntimeError, match='exited 7'):
        command([sys.executable, '-c', 'raise SystemExit(7)'], tmp_path / 'failure.log', time.monotonic() + 5)


def test_timeout_kills_child_even_when_group_leader_exits(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child = ('import os,signal,time; from pathlib import Path; '
             'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             f'Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)')
    parent = ('import subprocess,sys,time; '
              f'subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)')
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            command([sys.executable, '-c', parent], tmp_path / 'timeout.log',
                    time.monotonic() + 0.5, terminate_grace_seconds=0.1)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while True:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                   capture_output=True, text=True).stdout.strip()
            if not state or state.startswith('Z'):
                break
            assert time.monotonic() < deadline, 'owned child survived process-group cleanup'
            time.sleep(0.05)
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_missing_benchmark_evidence_fails_closed(tmp_path):
    with pytest.raises(ValueError, match='missing benchmark evidence'):
        check(tmp_path / 'models/autoports/model')


def performance_fixture(requests, output_tokens=128):
    return dict(completed=requests, failed=0, input_lens=[4096] * requests,
                output_lens=[output_tokens] * requests, total_input_tokens=4096 * requests,
                total_output_tokens=output_tokens * requests,
                **{field: 7 for field in PERFORMANCE_METRICS})


@pytest.mark.parametrize('field,value', [('input_lens', [512] * 8), ('output_lens', [1] * 8),
    ('total_input_tokens', 4095 * 8), ('mean_tpot_ms', float('nan')), ('completed', 7)])
def test_actual_performance_workload_is_validated(field, value):
    from benchmark_stage.evidence import validate_performance
    raw = performance_fixture(8)
    validate_performance(raw, 8, 128)
    raw[field] = value
    with pytest.raises(ValueError):
        validate_performance(raw, 8, 128)


def test_report_failure_preserves_original_client_error(tmp_path, monkeypatch):
    from benchmark_stage import run as runner
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'model': 'org/model', 'base_url': 'http://unused', 'performance_server_command': ['server-control'],
                                 'manifest': str(tmp_path / 'missing'), 'tasks': ['ifeval']}))
    def fail(*args):
        raise RuntimeError('original client error')
    monkeypatch.setattr(runner, 'command', fail)
    with pytest.raises(RuntimeError, match='original client error'):
        runner.run(config_path=config, output=tmp_path / 'run')
    summary = json.loads((tmp_path / 'run/summary.json').read_text())
    assert summary['status'] == 'failed'
    assert summary['error'] == 'RuntimeError: original client error'
    assert 'FileNotFoundError' in summary['report_error']



def server_fixture(capacity):
    return dict(model='org/model', implementation='models/autoports/model',
        generator_module='models.autoports.model.generator', prefix_caching=False,
        model_revision='123', tokenizer_revision='123', precision='accuracy', layer_count=32,
        configured_layer_count=32, source_commits={'tt-metal': '123'}, hardware='T3K',
        max_num_seqs=capacity, max_model_len=131072, base_url='http://unused',
        server_command=['vllm', 'serve', '--max-num-seqs', str(capacity)])


def write_server_fixture(output, capacity):
    import hashlib
    output.mkdir(parents=True, exist_ok=True)
    server = server_fixture(capacity)
    evidence = output / f'configuration-b{capacity}.log'
    evidence.write_text(f'Synthetic server startup: max_num_seqs={capacity}\n')
    server['configuration_evidence'] = evidence.name
    server['configuration_evidence_sha256'] = hashlib.sha256(evidence.read_bytes()).hexdigest()
    (output / f'perf-b{capacity}-server.json').write_text(json.dumps(server))
    return server


def fake_server_command(argv):
    if argv[0] != 'server-control':
        return False
    path = Path(argv[argv.index('--output') + 1])
    capacity = int(argv[argv.index('--max-num-seqs') + 1])
    write_server_fixture(path.parent, capacity)
    return True


def valid_gate_fixture(tmp_path):
    root = tmp_path / 'models/autoports/model'
    evidence = root / 'doc/benchmark'
    evidence.mkdir(parents=True)
    def write(path, data):
        path = evidence / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    for name in ('run/REPORT.md', 'RUN_NOTES.md'):
        (evidence / name).parent.mkdir(parents=True, exist_ok=True)
        (evidence / name).write_text('measured evidence')
    tasks = ['gsm8k_cot', 'ifeval']
    docs = [{'question': 'one'}, {'question': 'two'}]
    manifest = {'groups': {t: {'sample_count': 2, 'tasks': [t]} for t in tasks},
                'tasks': {t: {'indices': [0, 1], 'document_sha256': [digest(d) for d in docs]} for t in tasks}}
    manifest['manifest_sha256'] = digest(manifest)
    write('run/manifest.json', manifest)
    write('identity.json', server_fixture(32))
    write('run/run_config.json', dict(model='org/model', base_url='http://unused', tasks=tasks,
                                     budget_seconds=3600, output_tokens=128))
    summary = dict(status='completed', elapsed_seconds=30, accuracy={}, performance={})
    for task in tasks:
        stage = dict(model='org/model', subset_sha256=manifest['manifest_sha256'],
                     responses=2, expected_samples=2, concurrency=32, finish_reasons={'stop': 2})
        summary['accuracy'][task] = stage
        write(f'run/{task}/results.json', dict(results={task: {'acc,none': 0.5}}, benchmark_stage=stage))
        (evidence / f'run/{task}/samples_{task}.jsonl').write_text(''.join(json.dumps({'doc_id': i, 'doc': d}) + '\n' for i, d in enumerate(docs)))
        response = dict(choices=[dict(index=0, message=dict(content='answer'), finish_reason='stop')])
        (evidence / f'run/{task}/responses.jsonl').write_text((json.dumps(response) + '\n') * 2)
    for b in (1, 32):
        server = write_server_fixture(evidence / 'run', b)
        requests = max(8, b * 3)
        raw = dict(performance_fixture(requests), model_id='org/model', max_concurrency=b)
        summary['performance'][str(b)] = dict(raw, concurrency=b, server_max_num_seqs=b,
                                              server_identity_sha256=digest(server), requested_input_tokens=4096,
                                              requested_output_tokens=128, requests=requests)
        write(f'run/perf-b{b}.json', raw)
        write(f'run/perf-b{b}-warmup.json', dict(performance_fixture(b), model_id='org/model', max_concurrency=b))
    write('run/summary.json', summary)
    return root, evidence


@pytest.mark.parametrize('mutation', ['reference_metric', 'model', 'raw_perf', 'transcript', 'batch', 'layers', 'manifest', 'sample_ids', 'truncation'])
def test_gate_connects_results_to_raw_evidence(tmp_path, mutation):
    root, evidence = valid_gate_fixture(tmp_path)
    assert check(root, 'org/model') == evidence
    file, key, value = {
        'sample_ids': ('run/ifeval/samples_ifeval.jsonl', None, None),
        'truncation': ('run/ifeval/responses.jsonl', None, None),
        'reference_metric': ('run/run_config.json', None, None),
        'model': ('run/run_config.json', 'model', 'wrong/model'),
        'batch': ('run/summary.json', None, None),
        'layers': ('identity.json', 'layer_count', 1),
        'manifest': ('run/manifest.json', 'manifest_sha256', 'wrong'),
        'raw_perf': ('run/perf-b32.json', None, None),
        'transcript': ('run/ifeval/responses.jsonl', None, None),
    }[mutation]
    path = evidence / file
    if mutation in ('sample_ids', 'truncation'):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if mutation == 'sample_ids':
            rows[0]['doc_id'] = 3
        else:
            rows[0]['choices'][0]['finish_reason'] = 'length'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    elif mutation in ('raw_perf', 'transcript'):
        path.unlink()
    else:
        data = json.loads(path.read_text())
        if mutation == 'reference_metric':
            data['references'] = {'ifeval': {'nonexistent,none': {'score': 90, 'source_url': 'https://example.org/model'}}}
        elif mutation == 'batch':
            data['performance']['32']['concurrency'] = 1
        else:
            data[key] = value
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, OSError)):
        check(root, 'org/model')


@pytest.mark.parametrize('field', PERFORMANCE_METRICS)
def test_gate_requires_and_reconciles_all_performance_metrics(tmp_path, field):
    root, evidence = valid_gate_fixture(tmp_path)
    summary_path = evidence / 'run/summary.json'
    summary = json.loads(summary_path.read_text())
    summary['performance']['32'][field] += 1
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match='summary disagrees'):
        check(root, 'org/model')
    raw_path = evidence / 'run/perf-b32.json'
    raw = json.loads(raw_path.read_text())
    del raw[field]
    raw_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='missing/invalid performance'):
        check(root, 'org/model')


def test_gate_preserves_multiple_filters_per_question(tmp_path):
    root, evidence = valid_gate_fixture(tmp_path)
    path = evidence / 'run/gsm8k_cot/results.json'
    raw = json.loads(path.read_text())
    raw['results']['gsm8k_cot']['acc,second-filter'] = 0.5
    path.write_text(json.dumps(raw))
    path = evidence / 'run/gsm8k_cot/samples_gsm8k_cot.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows += [dict(row, filter='second-filter') for row in rows]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert check(root, 'org/model') == evidence
    rows.pop()
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(ValueError, match='document IDs'):
        check(root, 'org/model')


def test_reporting_budget_and_manifest_snapshot(tmp_path, monkeypatch):
    from benchmark_stage import run as runner, report
    source = tmp_path / 'source.json'
    original = {'manifest_sha256': 'fixture', 'groups': {}}
    source.write_text(json.dumps(original))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(model='org/model', base_url='http://unused', performance_server_command=['server-control'],
        manifest=str(source), tasks=[], budget_seconds=1)))
    clock = [0]
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    def completed(argv, log, deadline):
        if fake_server_command(argv): return
        count = int(argv[argv.index('--num-prompts') + 1])
        batch = int(argv[argv.index('--max-concurrency') + 1])
        path = Path(argv[argv.index('--result-dir') + 1]) / argv[argv.index('--result-filename') + 1]
        path.write_text(json.dumps(dict(performance_fixture(count), model_id='org/model', max_concurrency=batch)))
        source.write_text(json.dumps({'manifest_sha256': 'later-profile'}))
    monkeypatch.setattr(runner, 'command', completed)
    def slow_report(output, cfg, summary):
        assert json.loads(Path(cfg['manifest']).read_text()) == original
        clock[0] = 2
    monkeypatch.setattr(report, 'write_report', slow_report)
    with pytest.raises(RuntimeError, match='budget'):
        runner.run(config_path=config, output=tmp_path / 'run')
    summary = json.loads((tmp_path / 'run/summary.json').read_text())
    assert summary['status'] == 'failed'
    assert summary['elapsed_seconds'] == 2
    assert json.loads((tmp_path / 'run/manifest.json').read_text()) == original


def test_budget_failure_report_does_not_say_completed(tmp_path,monkeypatch):
 from benchmark_stage import run as runner,report
 source=tmp_path/'source.json';source.write_text(json.dumps({'manifest_sha256':'fixture','groups':{}}))
 cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'model':'org/model','base_url':'http://unused','performance_server_command':['server-control'],'manifest':str(source),'tasks':[],'budget_seconds':1}))
 clock=[0];monkeypatch.setattr(runner.time,'monotonic',lambda:clock[0])
 def command(argv,log,deadline):
  if fake_server_command(argv): return
  count=int(argv[argv.index('--num-prompts')+1]);batch=int(argv[argv.index('--max-concurrency')+1]);out=Path(argv[argv.index('--result-dir')+1])/argv[argv.index('--result-filename')+1]
  out.write_text(json.dumps(dict(performance_fixture(count),model_id='org/model',max_concurrency=batch)))
 monkeypatch.setattr(runner,'command',command);original=report.write_report
 def slow_report(output,config,summary):
  original(output,config,summary);clock[0]=2
 monkeypatch.setattr(report,'write_report',slow_report)
 with pytest.raises(RuntimeError,match='budget'):runner.run(config_path=cfg,output=tmp_path/'run')
 assert json.loads((tmp_path/'run/summary.json').read_text())['status']=='failed'
 assert 'Status: completed.' not in (tmp_path/'run/REPORT.md').read_text()


def exhausted_response(content=None, reasoning_key='reasoning'):
    return {'choices': [{'index': 0, 'message': {'content': content, reasoning_key: 'The answer is (A).'},
                         'finish_reason': 'length'}], 'usage': {'completion_tokens': 32768}}


@pytest.mark.parametrize('content', [None, '', '  '])
@pytest.mark.parametrize('reasoning_key', ['reasoning', 'reasoning_content'])
def test_exhausted_reasoning_retains_raw_and_never_scores_reasoning(content, reasoning_key, monkeypatch):
    from benchmark_stage.responses import scoring_response
    monkeypatch.setenv('LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER', 'The answer is (A).')
    raw = exhausted_response(content, reasoning_key)
    original = json.dumps(raw)
    normalized, exhausted = scoring_response(raw)
    assert exhausted and normalized['choices'][0]['message']['content'] == ''
    assert json.dumps(raw) == original


@pytest.mark.parametrize('reason', ['stop', 'length'])
def test_nonempty_final_is_preserved(reason):
    from benchmark_stage.responses import scoring_response
    raw = exhausted_response('Final answer')
    raw['choices'][0]['finish_reason'] = reason
    assert scoring_response(raw) == (raw, False)


@pytest.mark.parametrize('mutation', ['stop', 'no_reasoning', 'no_usage', 'bad_usage', 'no_content',
                                     'bad_content', 'bad_index', 'no_message', 'no_choices', 'two_choices', 'error'])
def test_invalid_final_responses_fail(mutation):
    from benchmark_stage.responses import scoring_response
    raw = exhausted_response()
    choice = raw['choices'][0]
    if mutation == 'stop': choice['finish_reason'] = 'stop'
    elif mutation == 'no_reasoning': del choice['message']['reasoning']
    elif mutation == 'no_usage': del raw['usage']
    elif mutation == 'bad_usage': raw['usage']['completion_tokens'] = True
    elif mutation == 'no_content': del choice['message']['content']
    elif mutation == 'bad_content': choice['message']['content'] = []
    elif mutation == 'bad_index': choice['index'] = 1
    elif mutation == 'no_message': del choice['message']
    elif mutation == 'no_choices': del raw['choices']
    elif mutation == 'two_choices': raw['choices'].append(choice.copy())
    else: raw['error'] = {'message': 'transport error'}
    with pytest.raises(ValueError): scoring_response(raw)


def test_gate_requires_exhaustion_count_without_accuracy_assessment(tmp_path):
    root, evidence = valid_gate_fixture(tmp_path)
    path = evidence / 'run/ifeval/responses.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0] = exhausted_response()
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    for path, stage in ((evidence / 'run/summary.json', 'summary'), (evidence / 'run/ifeval/results.json', 'raw')):
        data = json.loads(path.read_text())
        metadata = data['accuracy']['ifeval'] if stage == 'summary' else data['benchmark_stage']
        metadata['finish_reasons'] = {'stop': 1, 'length': 1}
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='exhausted final-answer count'): check(root)
    for path, stage in ((evidence / 'run/summary.json', 'summary'), (evidence / 'run/ifeval/results.json', 'raw')):
        data = json.loads(path.read_text())
        metadata = data['accuracy']['ifeval'] if stage == 'summary' else data['benchmark_stage']
        metadata['empty_final_length_responses'] = 1
        path.write_text(json.dumps(data))
    assert check(root) == evidence


def test_empty_exhausted_final_with_pinned_upstream_parser_and_ifeval(monkeypatch):
    pytest.importorskip('lm_eval')
    from importlib.metadata import version
    from lm_eval.models.openai_completions import LocalChatCompletion
    from lm_eval.tasks.ifeval import utils, instructions_registry
    from benchmark_stage.responses import scoring_response
    assert version('lm_eval') == '0.4.13'
    monkeypatch.setenv('LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER', 'The answer is (A).')
    backend = object.__new__(LocalChatCompletion)
    backend.think_end_token = None
    parsed = backend.parse_generations(scoring_response(exhausted_response())[0])
    assert parsed == ['']
    class PermissiveInstruction:
        def __init__(self, instruction_id): pass
        def build_description(self, **kwargs): pass
        def get_instruction_args(self): return []
        def check_following(self, response): return True
    monkeypatch.setitem(instructions_registry.INSTRUCTION_DICT, 'test:always', PermissiveInstruction)
    doc = dict(key=1, instruction_id_list=['test:always'], prompt='test', kwargs=[{}])
    scores = utils.process_results(doc, parsed)
    assert scores == dict(prompt_level_strict_acc=False, inst_level_strict_acc=[False],
                          prompt_level_loose_acc=False, inst_level_loose_acc=[False])
    assert utils.process_results(doc, ['control'])['prompt_level_strict_acc'] is True


@pytest.mark.parametrize('mutation', [None, 'link_id', 'doc_id', 'request_hash', 'scored_answer'])
def test_shared_gate_reconciles_requests_with_scored_answers(tmp_path, mutation):
    root, evidence = valid_gate_fixture(tmp_path)
    config_path = evidence / 'run/run_config.json'
    config = json.loads(config_path.read_text())
    config['accuracy_execution'] = 'shared'
    config_path.write_text(json.dumps(config))
    summary_path = evidence / 'run/summary.json'
    summary = json.loads(summary_path.read_text())
    summary['accuracy_execution'] = 'shared'
    for task in config['tasks']:
        meta = summary['accuracy'][task]
        meta.update(shared_groups=config['tasks'], timing_scope='shared accuracy pass')
        path = evidence / f'run/{task}/results.json'
        raw = json.loads(path.read_text()); raw['benchmark_stage'] = meta
        path.write_text(json.dumps(raw))
        sample_path = evidence / f'run/{task}/samples_{task}.jsonl'
        samples = [json.loads(line) for line in sample_path.read_text().splitlines()]
        responses, links = [], []
        for row in samples:
            row.update(arguments=[[f'question {row["doc_id"]}', {'until': []}]], resps=[['answer']])
            response_id = f'{task}-{row["doc_id"]}'
            responses.append(dict(id=response_id, choices=[dict(index=0, message=dict(content='answer'), finish_reason='stop')]))
            links.append(dict(group=task, task=task, doc_id=row['doc_id'], response_id=response_id,
                              request_sha256=digest(row['arguments'][0])))
        if task == 'ifeval':
            if mutation == 'link_id': links[0]['response_id'] = 'wrong'
            elif mutation == 'doc_id': links[0]['doc_id'] = 3
            elif mutation == 'request_hash': links[0]['request_sha256'] = 'wrong'
            elif mutation == 'scored_answer': samples[0]['resps'] = [['different answer']]
        for name, rows in [(sample_path.name, samples), ('responses.jsonl', responses), ('request_links.jsonl', links)]:
            (sample_path.parent / name).write_text(''.join(json.dumps(row) + '\n' for row in rows))
    summary_path.write_text(json.dumps(summary))
    if mutation:
        with pytest.raises(ValueError): check(root)
    else:
        assert check(root) == evidence


def test_jsonl_keeps_unicode_question_and_answer_separators(tmp_path):
    from benchmark_stage.responses import read_jsonl
    rows = [{'doc': {'question': 'one\u2028two\u2029three\u0085four'}},
            {'choices': [{'message': {'content': 'answer\u2028continued'}}]}]
    path = tmp_path / 'samples.jsonl'
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    assert read_jsonl(path) == rows


def test_low_scores_and_missing_references_complete_without_review(tmp_path):
    root, evidence = valid_gate_fixture(tmp_path)
    path = evidence / 'run/ifeval/results.json'
    raw = json.loads(path.read_text())
    raw['results']['ifeval']['acc,none'] = 0.0
    path.write_text(json.dumps(raw))
    config_path = evidence / 'run/run_config.json'
    config = json.loads(config_path.read_text())
    config['references'] = {'ifeval': {'acc,none': {'score': 99, 'source_url': 'https://example.org/model'}}}
    config_path.write_text(json.dumps(config))
    assert not (evidence / 'accuracy_review.json').exists()
    assert check(root) == evidence  # 99-point gap and no reference for GSM8K are valid results.


@pytest.mark.parametrize('reference', [None, {'score': 70, 'source_url': 'https://example.org/model'}])
def test_report_has_measured_scores_references_and_performance_without_verdict(tmp_path, reference):
    from benchmark_stage.report import write_report
    root, evidence = valid_gate_fixture(tmp_path)
    run = evidence / 'run'
    manifest = json.loads((run / 'manifest.json').read_text())
    for group in manifest['groups'].values():
        group['population'] = 100
    (run / 'manifest.json').write_text(json.dumps(manifest))
    config = json.loads((run / 'run_config.json').read_text())
    config['manifest'] = str(run / 'manifest.json')
    if reference:
        config['references'] = {'ifeval': {'acc,none': reference}}
    summary = json.loads((run / 'summary.json').read_text())
    write_report(run, config, summary)
    report = (run / 'REPORT.md').read_text()
    assert '| 2 / 100 | acc,none | 50.00 |' in report
    assert '| 70.00 | -20.00 | [reference](https://example.org/model)' in report if reference else 'Unavailable' in report
    assert '| Single user | 1 | 1 | 4096 / 128 |' in report and '| 32 users | 32 | 32 | 4096 / 128 |' in report
    assert 'verdict' not in report and 'separate accuracy review' not in report
    assert report.index('| Profile | Concurrent requests') < report.index('## Run details')


def roofline_fixture(run):
    import hashlib
    (run / 'phase-timings.json').write_text('{"source": "synthetic full-phase timing fixture"}')
    data = {'1': {
        'performance_sha256': hashlib.sha256((run / 'perf-b1.json').read_bytes()).hexdigest(),
        'prefill': dict(flops=2000, seconds=2, peak_flops_per_second=2000),
        'decode': dict(dram_bytes=3000, seconds=3, peak_dram_bytes_per_second=2000)}}
    for phase in ('prefill', 'decode'):
        data['1'][phase].update(timing_scope='full_phase_wall_time', timing_method='Full host interval',
            work_method='Synthetic accounting fixture', peak_source='Synthetic peak fixture',
            evidence='phase-timings.json')
    (run / 'roofline.json').write_text(json.dumps(data))
    return data


def test_roofline_uses_full_phase_time_and_is_linked_to_perf_run(tmp_path):
    from benchmark_stage.roofline import load_roofline
    root, evidence = valid_gate_fixture(tmp_path)
    run = evidence / 'run'
    data = roofline_fixture(run)
    result = load_roofline(run)
    assert result['1']['prefill']['percent'] == 50
    assert result['1']['decode']['percent'] == 50
    assert check(root) == evidence
    # Doubling the full elapsed duration halves utilization, independent of matmul time.
    data['1']['prefill']['seconds'] = 4
    (run / 'roofline.json').write_text(json.dumps(data))
    assert load_roofline(run)['1']['prefill']['percent'] == 25


@pytest.mark.parametrize('mutation', ['matmul_only', 'overlap', 'wrong_run', 'missing_evidence', 'bad_number'])
def test_roofline_rejects_invalid_measurement_inputs(tmp_path, mutation):
    from benchmark_stage.roofline import load_roofline
    root, evidence = valid_gate_fixture(tmp_path)
    run = evidence / 'run'
    data = roofline_fixture(run)
    entry = data['1']['prefill']
    if mutation == 'matmul_only': entry['timing_scope'] = 'matmul_duration'
    elif mutation == 'overlap': entry['seconds'] = 20  # More than complete performance wall time.
    elif mutation == 'wrong_run': data['1']['performance_sha256'] = 'unrelated'
    elif mutation == 'missing_evidence': entry['evidence'] = 'missing.json'
    else: entry['peak_flops_per_second'] = float('nan')
    (run / 'roofline.json').write_text(json.dumps(data))
    with pytest.raises(ValueError): load_roofline(run)


@pytest.mark.parametrize('collect', [True, False])
def test_runner_collects_roofline_before_reporting_with_same_deadline(tmp_path, monkeypatch, collect):
    from benchmark_stage import run as runner
    source = tmp_path / 'manifest.json'
    source.write_text(json.dumps({'manifest_sha256': 'fixture', 'groups': {}}))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(dict(model='org/model', base_url='http://unused', performance_server_command=['server-control'],
        manifest=str(source), tasks=[], roofline_command=['collector'])))
    deadlines = []
    def completed(argv, log, deadline):
        deadlines.append(deadline)
        if fake_server_command(argv): return
        if argv[0] == 'collector':
            assert argv[1] == '--run-dir'
            if collect:
                roofline_fixture(Path(argv[2]))
            return
        count = int(argv[argv.index('--num-prompts') + 1])
        batch = int(argv[argv.index('--max-concurrency') + 1])
        path = Path(argv[argv.index('--result-dir') + 1]) / argv[argv.index('--result-filename') + 1]
        path.write_text(json.dumps(dict(performance_fixture(count), model_id='org/model', max_concurrency=batch)))
    monkeypatch.setattr(runner, 'command', completed)
    if collect:
        assert runner.run(config_path=config_path, output=tmp_path / 'run')['status'] == 'completed'
        report = (tmp_path / 'run/REPORT.md').read_text()
        assert '| 50.00 | 50.00 |' in report
        assert 'phase-timings.json' in report
    else:
        with pytest.raises(ValueError, match='did not write'):
            runner.run(config_path=config_path, output=tmp_path / 'run')
        assert json.loads((tmp_path / 'run/summary.json').read_text())['status'] == 'failed'
    assert len(deadlines) == 7 and len(set(deadlines)) == 1


@pytest.mark.parametrize('mutation', ['capacity', 'launch_command', 'precision', 'context', 'evidence', 'summary'])
def test_gate_requires_real_single_user_profile(tmp_path, mutation):
    root, evidence = valid_gate_fixture(tmp_path)
    path = evidence / 'run/perf-b1-server.json'
    server = json.loads(path.read_text())
    if mutation == 'capacity': server['max_num_seqs'] = 32
    elif mutation == 'launch_command': server['server_command'][-1] = '32'
    elif mutation == 'precision': server['precision'] = 'lower precision'
    elif mutation == 'context': server['max_model_len'] = 8192
    elif mutation == 'evidence':
        (path.parent / server['configuration_evidence']).write_text('changed source evidence')
    path.write_text(json.dumps(server))
    summary_path = evidence / 'run/summary.json'
    summary = json.loads(summary_path.read_text())
    summary['performance']['1']['server_identity_sha256'] = digest(server)
    if mutation == 'summary': summary['performance']['1']['server_max_num_seqs'] = 32
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError):
        check(root)


@pytest.mark.parametrize('wrong_capacity', [False, True])
def test_runner_switches_server_capacity_before_each_performance_profile(tmp_path, monkeypatch, wrong_capacity):
    from benchmark_stage import run as runner
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'manifest_sha256': 'fixture', 'groups': {}}))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(dict(model='org/model', base_url='http://unused',
        manifest=str(manifest), tasks=[], performance_server_command=['server-control'])))
    events = []
    def completed(argv, log, deadline):
        if argv[0] == 'server-control':
            capacity = int(argv[argv.index('--max-num-seqs') + 1])
            path = Path(argv[argv.index('--output') + 1])
            events.append(('server', capacity))
            server = write_server_fixture(path.parent, capacity)
            if wrong_capacity and capacity == 1:
                server['max_num_seqs'] = 32
                server['server_command'][-1] = '32'
                path.write_text(json.dumps(server))
            return
        capacity = int(argv[argv.index('--max-concurrency') + 1])
        count = int(argv[argv.index('--num-prompts') + 1])
        events.append(('measure', capacity))
        path = Path(argv[argv.index('--result-dir') + 1]) / argv[argv.index('--result-filename') + 1]
        path.write_text(json.dumps(dict(performance_fixture(count), model_id='org/model', max_concurrency=capacity)))
    monkeypatch.setattr(runner, 'command', completed)
    if wrong_capacity:
        with pytest.raises(ValueError, match='1-slot server'):
            runner.run(config_path=config_path, output=tmp_path / 'run')
        assert events == [('server', 32), ('measure', 32), ('measure', 32), ('server', 1)]
        assert not (tmp_path / 'run/perf-b1.json').exists()
    else:
        summary = runner.run(config_path=config_path, output=tmp_path / 'run')
        assert events == [('server', 32), ('measure', 32), ('measure', 32),
                          ('server', 1), ('measure', 1), ('measure', 1)]
        assert summary['performance']['1']['server_max_num_seqs'] == 1
        assert summary['performance']['32']['server_max_num_seqs'] == 32
        report = (tmp_path / 'run/REPORT.md').read_text()
        assert report.index('| Single user | 1 | 1 |') < report.index('| 32 users | 32 | 32 |')


def test_runner_rejects_implicit_shared_server_before_accuracy(tmp_path, monkeypatch):
    from benchmark_stage import run as runner
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'manifest_sha256': 'fixture', 'groups': {}}))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(model='org/model', base_url='http://unused',
        manifest=str(manifest), tasks=[])))
    def unexpected(*args):
        pytest.fail('client ran before server-profile configuration was checked')
    monkeypatch.setattr(runner, 'command', unexpected)
    with pytest.raises(ValueError, match='performance_server_command is required'):
        runner.run(config_path=config, output=tmp_path / 'run')
