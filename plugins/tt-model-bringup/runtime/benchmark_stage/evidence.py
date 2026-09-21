"""Validate measured workloads and resolve upstream accuracy metrics."""
from __future__ import annotations

import math


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
    for field in ('mean_ttft_ms', 'mean_tpot_ms', 'mean_itl_ms', 'output_throughput'):
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
