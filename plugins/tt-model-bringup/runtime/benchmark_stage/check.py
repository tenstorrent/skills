"""Offline evidence gate for the final bringup benchmark stage."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from benchmark_stage.answers import advisories as answer_advisories, task_statistics
from benchmark_stage.evidence import PERFORMANCE_METRICS, benchmark_rows, finite_number, validate_performance, validate_server
from benchmark_stage.plan import INPUT_TOKENS, batch_plan, lone_user_profiles, perf_requests, validate_sample_reduction
from benchmark_stage.roofline import load_roofline
from benchmark_stage.subsets import digest
from benchmark_stage.responses import read_jsonl, scoring_response


def read(path):
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f'missing benchmark evidence: {path}')
    return json.loads(path.read_text())


def check(model_dir, hf_model='', advisories=None):
    """Raise ValueError on a critical defect; append advisory lines to ``advisories``."""
    advisories = [] if advisories is None else advisories
    root = Path(model_dir)
    evidence = root / 'doc/benchmark'
    for filename in ('run/REPORT.md', 'RUN_NOTES.md'):
        path = evidence / filename
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'missing benchmark evidence: {path}')
    identity = read(evidence / 'identity.json')
    target = root.as_posix().split('models/autoports/')
    if len(target) != 2:
        raise ValueError('bringup gate requires a models/autoports target; calibration is separate')
    target = 'models/autoports/' + target[1].rstrip('/')
    if identity.get('implementation') != target:
        raise ValueError('evaluated implementation does not match the target autoport')
    if not identity.get('generator_module', '').startswith(target.replace('/', '.') + '.'):
        raise ValueError('imported generator is not inside the target autoport')
    if hf_model and identity.get('model') != hf_model:
        raise ValueError('evaluated model does not match HF_MODEL')
    for key in ('model', 'model_revision', 'tokenizer_revision', 'precision', 'source_commits', 'hardware', 'server_command'):
        if not identity.get(key):
            raise ValueError(f'missing model identity field: {key}')
    layers = identity.get('layer_count')
    if not isinstance(layers, int) or layers < 1 or layers != identity.get('configured_layer_count'):
        raise ValueError('identity must verify all configured model layers')
    if type(identity.get('prefix_caching')) is not bool:
        raise ValueError('identity must record prefix_caching as true or false')
    summary = read(evidence / 'run/summary.json')
    config = read(evidence / 'run/run_config.json')
    if config.get('model') != identity['model']:
        raise ValueError('run model does not match evaluated implementation identity')
    plan = batch_plan(config)
    for key, value in plan.items():
        if summary.get(key) != value:
            raise ValueError(f'summary {key} differs from the run configuration')
    if summary.get('status') != 'completed':
        raise ValueError('benchmark stage did not complete')
    elapsed, client, setup = (summary.get(k) for k in ('elapsed_seconds', 'client_seconds', 'setup_seconds'))
    if not all(finite_number(v) and v >= 0 for v in (elapsed, client, setup)) or abs(client + setup - elapsed) > 1e-6 * max(1, elapsed):
        raise ValueError('summary must record client, setup and total wall time')
    if not 0 < client < plan['client_budget_seconds']:
        raise ValueError('client clock exceeded its budget of 30 minutes per configured batch size')
    if not setup < plan['setup_budget_seconds']:
        raise ValueError('server setup (launch, compile, warmup) exceeded its budget')
    measured = plan['measured_batch_sizes']
    accuracy_concurrency = plan['accuracy_concurrency']
    manifest = read(evidence / 'run/manifest.json')
    if digest({k: v for k, v in manifest.items() if k != 'manifest_sha256'}) != manifest['manifest_sha256']:
        raise ValueError('subset manifest checksum mismatch')
    validate_sample_reduction(config, manifest)
    accuracy = summary.get('accuracy', {})
    execution = config.get('accuracy_execution', 'sequential')
    if execution not in ('sequential', 'shared') or summary.get('accuracy_execution', 'sequential') != execution:
        raise ValueError('accuracy execution mode differs from run configuration')
    if not accuracy or set(accuracy) != set(config['tasks']):
        raise ValueError('missing required accuracy tasks')
    results = {}
    for task, result in accuracy.items():
        group = manifest['groups'][task]
        if result.get('subset_sha256') != manifest['manifest_sha256']:
            raise ValueError(f'{task}: subset mismatch')
        if result.get('responses') != group['sample_count'] or result.get('expected_samples') != group['sample_count'] or result.get('concurrency') != accuracy_concurrency:
            raise ValueError(f'{task}: incomplete accuracy requests or wrong concurrency')
        if result.get('model') != identity['model']:
            raise ValueError(f'{task}: accuracy model mismatch')
        if result.get('generation_overrides', {}) != config.get('generation', {}).get(task, {}):
            raise ValueError(f'{task}: generation overrides differ from run configuration')
        raw = read(evidence / 'run' / task / 'results.json')
        if raw.get('benchmark_stage') != result or not raw.get('results'):
            raise ValueError(f'{task}: summary disagrees with raw accuracy result')
        transcript = evidence / 'run' / task / 'responses.jsonl'
        responses = read_jsonl(transcript)
        if len(responses) != group['sample_count']:
            raise ValueError(f'{task}: incomplete response transcript')
        empty_finals = sum(scoring_response(response)[1] for response in responses)
        if result.get('empty_final_length_responses', 0) != empty_finals:
            raise ValueError(f'{task}: summary disagrees with exhausted final-answer count')
        linked = {}
        if execution == 'shared':
            if result.get('shared_groups') != config['tasks'] or result.get('timing_scope') != 'shared accuracy pass':
                raise ValueError(f'{task}: missing shared accuracy identity')
            links = read_jsonl(transcript.parent / 'request_links.jsonl')
            if len(links) != len(responses):
                raise ValueError(f'{task}: incomplete request links')
            # Identical prompts on different documents are legitimate; the
            # identity is the frozen document instance, not the request text.
            if len({row.get('response_id') for row in links}) != len(links) or len({row.get('request_instance_sha256') for row in links}) != len(links):
                raise ValueError(f'{task}: repeated request/response identity')
            for link, response in zip(links, responses):
                key = (link.get('task'), link.get('doc_id'))
                if key in linked or link.get('group') != task or key[0] not in group['tasks'] or not response.get('id') or link.get('response_id') != response['id']:
                    raise ValueError(f'{task}: invalid request/response identity')
                if link.get('request_instance_sha256') != digest([*key, link.get('request_sha256')]):
                    raise ValueError(f'{task}: invalid request instance content hash')
                linked[key] = (link, scoring_response(response)[0]['choices'][0]['message']['content'])
        reasons = {}
        for response in responses:
            reason = response['choices'][0].get('finish_reason', 'missing')
            reasons[reason] = reasons.get(reason, 0) + 1
        if result.get('finish_reasons') != reasons:
            raise ValueError(f'{task}: summary disagrees with response finish reasons')
        if set(reasons) - {'stop', 'length'}:
            raise ValueError(f'{task}: invalid/missing API finish reason')
        for child in group['tasks']:
            frozen = manifest['tasks'][child]
            samples = read_jsonl(evidence / 'run' / task / f'samples_{child}.jsonl')
            filters = {key.rsplit(',', 1)[1] for key in raw['results'][child] if ',' in key and '_stderr' not in key}
            if {row.get('filter', 'none') for row in samples} != filters:
                raise ValueError(f'{child}: missing scored sample filters')
            for filter_name in filters:
                ids = sorted(row['doc_id'] for row in samples if row.get('filter', 'none') == filter_name)
                if ids != frozen['indices']:
                    raise ValueError(f'{child}: scored document IDs differ from the frozen subset')
            hashes = dict(zip(frozen['indices'], frozen['document_sha256']))
            if any(digest(row['doc']) != hashes[row['doc_id']] for row in samples):
                raise ValueError(f'{child}: scored documents differ from the frozen subset')
            if execution == 'shared':
                for row in samples:
                    link, content = linked.get((child, row['doc_id']), ({}, None))
                    args = row.get('arguments', [])
                    if len(args) != 1 or digest(args[0]) != link.get('request_sha256') or row.get('resps') != [[content]]:
                        raise ValueError(f'{child}: scored answer disagrees with linked API request/response')
        results[task] = raw
    osl = config.get('output_tokens', 128)
    if not isinstance(osl, int) or osl < 2:
        raise ValueError('performance output length must permit decode timing')
    if set(summary.get('performance', {})) != {str(b) for b in measured}:
        raise ValueError('summary performance rows differ from the measured batch sizes')
    servers = {}
    for size in measured:
        batch = str(size)
        result = summary['performance'][batch]
        server = read(evidence / 'run' / f'perf-b{batch}-server.json')
        validate_server(server, size, config['model'], config['base_url'],
                        baseline=identity, output=evidence / 'run')
        servers[size] = server
        if result.get('server_max_num_seqs') != size or result.get('server_identity_sha256') != digest(server):
            raise ValueError(f'batch {batch}: summary disagrees with performance server identity')
        if result.get('server_prefix_caching') != server['prefix_caching']:
            raise ValueError(f'batch {batch}: summary disagrees with the server prefix-caching setting')
        requests = perf_requests(size)
        if result.get('requested_input_tokens') != INPUT_TOKENS or result.get('requested_output_tokens') != osl or result.get('requests') != requests or result.get('concurrency') != size:
            raise ValueError(f'batch {batch}: wrong performance workload')
        for warmup in (True, False):
            name = f'perf-b{batch}' + ('-warmup' if warmup else '')
            raw = read(evidence / 'run' / f'{name}.json')
            validate_performance(raw, size if warmup else requests, osl)
            if raw.get('model_id') != identity['model'] or raw.get('max_concurrency') != size:
                raise ValueError(f'batch {batch}: raw performance identity/concurrency mismatch')
            if not warmup:
                for key in ('completed', 'total_input_tokens', 'total_output_tokens', *PERFORMANCE_METRICS):
                    if result.get(key) != raw.get(key):
                        raise ValueError(f'batch {batch}: summary disagrees with raw {key}')
    largest = servers[accuracy_concurrency]
    if summary.get('accuracy_prefix_caching') != largest['prefix_caching']:
        raise ValueError('summary disagrees with the accuracy server prefix-caching setting')
    lone = summary.get('lone_user', {})
    profiles = lone_user_profiles(accuracy_concurrency, largest['max_model_len'], osl,
                                  config.get('long_context_input_tokens', 32768))
    if set(lone) != {p['name'] for p in profiles}:
        raise ValueError('missing concurrency-1 rows (functional 128/128 and long context) on the largest server')
    for profile in profiles:
        result = lone[profile['name']]
        raw = read(evidence / 'run' / (profile['file'] + '.json'))
        validate_performance(raw, profile['requests'], osl, profile['input_tokens'])
        if raw.get('model_id') != identity['model'] or raw.get('max_concurrency') != 1:
            raise ValueError(f"{profile['name']}: raw performance identity/concurrency mismatch")
        if (result.get('server_identity_sha256') != digest(largest) or result.get('concurrency') != 1
                or result.get('requested_input_tokens') != profile['input_tokens'] or result.get('requests') != profile['requests']):
            raise ValueError(f"{profile['name']}: wrong lone-user workload or server")
        for key in ('completed', 'total_input_tokens', 'total_output_tokens', *PERFORMANCE_METRICS):
            if result.get(key) != raw.get(key):
                raise ValueError(f"{profile['name']}: summary disagrees with raw {key}")
    if set(config.get('references', {})) - set(accuracy) or set(config.get('metrics', {})) - set(accuracy):
        raise ValueError('report metrics/references name a task that was not run')
    for task, raw in results.items():
        benchmark_rows(config, manifest, task, raw)
    load_roofline(evidence / 'run', required=tuple(str(b) for b in measured))
    stats = summary.get('answer_statistics', {})
    for task in config['tasks']:
        if task not in stats:
            raise ValueError(f'{task}: missing answer statistics (extraction, blank-at-cap, answer tokens)')
        if stats[task] != task_statistics(evidence / 'run', config, manifest, task):
            raise ValueError(f'{task}: answer statistics disagree with raw responses and scored samples')
        advisories.extend(answer_advisories(task, stats[task]))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--hf-model', default='')
    args = parser.parse_args()
    advisories = []
    try:
        evidence = check(args.model_dir, args.hf_model, advisories)
    except (ValueError, KeyError, OSError) as exc:
        print(f'CRITICAL {exc}')
        raise SystemExit(2)
    for line in advisories[:30]:
        print(f'ADVISORY {line}')
    if advisories:
        print(f'Benchmark evidence valid with {len(advisories)} advisory finding(s): {evidence}')
        raise SystemExit(1)
    print(f'Benchmark evidence passed: {evidence}')


if __name__ == '__main__':
    main()
