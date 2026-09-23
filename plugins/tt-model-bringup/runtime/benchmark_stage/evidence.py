"""Validate measured workloads and resolve upstream accuracy metrics."""
from __future__ import annotations

import math

PERFORMANCE_METRICS = (
    'duration', 'request_throughput', 'output_throughput', 'total_token_throughput',
    *(f'{stat}_{metric}_ms' for metric in ('ttft', 'tpot', 'itl', 'e2el')
      for stat in ('mean', 'median', 'p95', 'p99')),
)


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
