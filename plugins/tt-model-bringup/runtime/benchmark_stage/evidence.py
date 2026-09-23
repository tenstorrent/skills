"""Validate measured workloads and resolve upstream accuracy metrics."""
from __future__ import annotations

import math
import hashlib
from pathlib import Path

PERFORMANCE_METRICS = (
    'duration', 'request_throughput', 'output_throughput', 'total_token_throughput',
    *(f'{stat}_{metric}_ms' for metric in ('ttft', 'tpot', 'itl', 'e2el')
      for stat in ('mean', 'median', 'p95', 'p99')),
)

SERVER_FIELDS = ('model', 'implementation', 'generator_module', 'model_revision',
                 'tokenizer_revision', 'precision', 'layer_count', 'configured_layer_count',
                 'source_commits', 'hardware', 'max_model_len')


def validate_server(server, capacity, model, base_url, *, baseline=None, output=None):
    """Check recorded server capacity independently of client concurrency."""
    if server.get('max_num_seqs') != capacity or type(server.get('max_num_seqs')) is not int:
        raise ValueError(f'performance requires a {capacity}-slot server')
    if server.get('model') != model or server.get('base_url') != base_url:
        raise ValueError('performance server model/endpoint mismatch')
    if server.get('prefix_caching') is not False:
        raise ValueError('performance server must disable prefix caching')
    for field in SERVER_FIELDS:
        if not server.get(field):
            raise ValueError(f'missing performance server identity: {field}')
        if baseline is not None and server[field] != baseline.get(field):
            raise ValueError(f'performance server changed {field}')
    if (type(server['layer_count']) is not int or server['layer_count'] < 1
            or server['layer_count'] != server['configured_layer_count']):
        raise ValueError('performance server must run all configured layers')
    if type(server['max_model_len']) is not int or server['max_model_len'] < 4096:
        raise ValueError('invalid performance server context capacity')
    argv = server.get('server_command', [])
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError('server_command must contain the observed launch arguments')
    values = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == '--max-num-seqs']
    values += [arg.split('=', 1)[1] for arg in argv if arg.startswith('--max-num-seqs=')]
    if values != [str(capacity)]:
        raise ValueError('server launch command does not match recorded capacity')
    if output is not None:
        artifact = (Path(output) / server.get('configuration_evidence', '')).resolve()
        if not artifact.is_relative_to(Path(output).resolve()) or not artifact.is_file() or not artifact.stat().st_size:
            raise ValueError('missing performance server configuration evidence')
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if server.get('configuration_evidence_sha256') != actual:
            raise ValueError('performance server configuration evidence changed')


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_performance(raw, requests, output_tokens):
    if raw.get('completed') != requests or raw.get('failed', 0):
        raise ValueError('incomplete performance requests')
    for field, length in (('input_lens', 4096), ('output_lens', output_tokens)):
        values = raw.get(field, [])
        if len(values) != requests or any(v != length for v in values):
            raise ValueError(f'performance {field}: expected {requests} requests of {length} tokens')
    if raw.get('total_input_tokens') != requests * 4096 or raw.get('total_output_tokens') != requests * output_tokens:
        raise ValueError('performance total token counts disagree with requested lengths')
    for field in PERFORMANCE_METRICS:
        value = raw.get(field)
        if not finite_number(value) or value <= 0:
            raise ValueError(f'missing/invalid performance {field}')


def metric_score(result, task, metric, *, expected_children=None):
    rows = result.get('groups', {}).get(task, result.get('results', {}).get(task, {}))
    if isinstance(metric, str) and metric.startswith('subject_macro:'):
        child_metric = metric.split(':', 1)[1]
        children = result.get('group_subtasks', {}).get(task, [])
        if (not expected_children or not children or len(children) != len(set(children))
                or set(children) != set(expected_children)):
            raise ValueError(f'{task}: subject-macro membership differs from frozen group')
        values = [result.get('results', {}).get(child, {}).get(child_metric) for child in children]
    elif metric == 'ifeval_mean_four':
        keys = ['prompt_level_strict_acc,none', 'inst_level_strict_acc,none',
                'prompt_level_loose_acc,none', 'inst_level_loose_acc,none']
        values = [rows.get(k) for k in keys]
    else:
        values = [rows.get(metric)]
    if any(not finite_number(v) or not 0 <= v <= 1 for v in values):
        raise ValueError(f'{task}: missing/invalid measured metric {metric}')
    return 100 * sum(values) / len(values)


def benchmark_rows(config, manifest, task, result):
    """Resolve report metrics and optional references; impose no accuracy threshold."""
    raw = result.get('groups', {}).get(task, result.get('results', {}).get(task, {}))
    references = config.get('references', {}).get(task, {})
    metrics = config.get('metrics', {}).get(task) or list(references) or [
        key for key in raw if ',' in key and '_stderr' not in key]
    if not metrics or len(metrics) != len(set(metrics)):
        raise ValueError(f'{task}: missing or repeated report metrics')
    if set(references) - set(metrics):
        raise ValueError(f'{task}: reference metric is absent from the report')
    rows = []
    for metric in metrics:
        score = metric_score(result, task, metric, expected_children=manifest['groups'][task]['tasks'])
        ref = references.get(metric)
        if ref is not None:
            if (not finite_number(ref.get('score')) or not 0 <= ref['score'] <= 100
                    or not ref.get('source_url', '').startswith('https://')):
                raise ValueError(f'{task}: published reference needs a percentage and source URL')
        rows.append((metric, score, ref))
    return rows
