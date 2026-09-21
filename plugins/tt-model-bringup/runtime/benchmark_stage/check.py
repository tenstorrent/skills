"""Offline evidence gate for the final bringup benchmark stage."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

from benchmark_stage.evidence import PERFORMANCE_METRICS, finite_number, metric_score, validate_performance
from benchmark_stage.subsets import digest
from benchmark_stage.responses import read_jsonl, scoring_response


def read(path):
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f'missing benchmark evidence: {path}')
    return json.loads(path.read_text())


def check(model_dir, hf_model=''):
    root = Path(model_dir)
    evidence = root / 'doc/benchmark'
    for filename in ('REPORT.md', 'RUN_NOTES.md'):
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
    if identity.get('prefix_caching') is not False:
        raise ValueError('performance evidence must disable prefix caching')
    summary = read(evidence / 'run/summary.json')
    config = read(evidence / 'run/run_config.json')
    if config.get('model') != identity['model']:
        raise ValueError('run model does not match evaluated implementation identity')
    elapsed = summary.get('elapsed_seconds')
    if summary.get('status') != 'completed' or not finite_number(elapsed) or not 0 < elapsed < min(config.get('budget_seconds', 3600), 3600):
        raise ValueError('benchmark stage did not complete within its budget and one hour')
    manifest = read(evidence / 'manifest.json')
    if digest({k: v for k, v in manifest.items() if k != 'manifest_sha256'}) != manifest['manifest_sha256']:
        raise ValueError('subset manifest checksum mismatch')
    accuracy = summary.get('accuracy', {})
    execution = config.get('accuracy_execution', 'sequential')
    if execution not in ('sequential', 'shared') or summary.get('accuracy_execution', 'sequential') != execution:
        raise ValueError('accuracy execution mode differs from run configuration')
    if len(accuracy) < 2 or set(accuracy) != set(config['tasks']):
        raise ValueError('missing required accuracy tasks')
    results = {}
    for task, result in accuracy.items():
        group = manifest['groups'][task]
        if result.get('subset_sha256') != manifest['manifest_sha256']:
            raise ValueError(f'{task}: subset mismatch')
        if result.get('responses') != group['sample_count'] or result.get('expected_samples') != group['sample_count'] or result.get('concurrency') != 32:
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
            if len({row.get('response_id') for row in links}) != len(links) or len({row.get('request_sha256') for row in links}) != len(links):
                raise ValueError(f'{task}: repeated request/response identity')
            for link, response in zip(links, responses):
                key = (link.get('task'), link.get('doc_id'))
                if key in linked or link.get('group') != task or key[0] not in group['tasks'] or not response.get('id') or link.get('response_id') != response['id']:
                    raise ValueError(f'{task}: invalid request/response identity')
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
    for batch in ('1', '32'):
        result = summary.get('performance', {}).get(batch, {})
        requests = max(8, int(batch) * 3)
        if result.get('requested_input_tokens') != 4096 or result.get('requested_output_tokens') != osl or result.get('requests') != requests or result.get('concurrency') != int(batch):
            raise ValueError(f'batch {batch}: wrong performance workload')
        for warmup in (True, False):
            name = f'perf-b{batch}' + ('-warmup' if warmup else '')
            raw = read(evidence / 'run' / f'{name}.json')
            validate_performance(raw, int(batch) if warmup else requests, osl)
            if raw.get('model_id') != identity['model'] or raw.get('max_concurrency') != int(batch):
                raise ValueError(f'batch {batch}: raw performance identity/concurrency mismatch')
            if not warmup:
                for key in ('completed', 'total_input_tokens', 'total_output_tokens', *PERFORMANCE_METRICS):
                    if result.get(key) != raw.get(key):
                        raise ValueError(f'batch {batch}: summary disagrees with raw {key}')
    review = read(evidence / 'accuracy_review.json')
    if review.get('verdict') != 'pass' or set(review.get('benchmarks', {})) != set(accuracy):
        raise ValueError('accuracy comparison has not passed review for every task')
    for task, entry in review['benchmarks'].items():
        if not entry.get('source_url', '').startswith('https://') or not entry.get('assessment'):
            raise ValueError(f'{task}: incomplete published-reference assessment')
        if accuracy[task].get('finish_reasons', {}).get('length') and not entry.get('truncation_assessment'):
            raise ValueError(f'{task}: length-limited answers require a truncation assessment or rerun')
        score = metric_score(results[task], task, entry.get('reference_metric'),
                             expected_children=manifest['groups'][task]['tasks'])
        for key in ('reference_score', 'subset_score', 'delta'):
            if not finite_number(entry.get(key)):
                raise ValueError(f'{task}: invalid review {key}')
        if not 0 <= entry['reference_score'] <= 100 or not math.isclose(entry['subset_score'], score, abs_tol=0.005) or not math.isclose(entry['delta'], score - entry['reference_score'], abs_tol=0.005):
            raise ValueError(f'{task}: published comparison contradicts measured score')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--hf-model', default='')
    args = parser.parse_args()
    try:
        evidence = check(args.model_dir, args.hf_model)
    except (ValueError, KeyError, OSError) as exc:
        print(exc)
        raise SystemExit(2)
    print(f'Benchmark evidence passed: {evidence}')


if __name__ == '__main__':
    main()
