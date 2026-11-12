"""Per-task answer statistics: extraction, blank answers at the cap, answer length.

Computed from the raw responses and scored samples the stage already writes.
They show whether a low score is the model or the protocol: an answer the
scorer cannot extract, or a reasoning trace cut at the output cap, scores zero
either way. Thresholds are defaults the owner can tune.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from benchmark_stage.responses import read_jsonl

EXTRACTION_TARGET = 0.95
BLANK_AT_CAP_LIMIT = 0.05
# lm-eval's LocalChatCompletion default when neither the task nor the run sets one.
BACKEND_DEFAULT_CAP = 2048
INVALID = '[invalid]'


def nearest_rank(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def recommended_cap(p90, current):
    """p90 x 1.5 rounded up to a power-of-two K, never below the current cap."""
    need = math.ceil(p90 * 1.5)
    cap = 1024
    while cap < need:
        cap *= 2
    return max(cap, current)


def output_cap(config, task, raw):
    """The output cap the run used for this task."""
    override = config.get('generation', {}).get(task, {}).get('max_gen_toks')
    if isinstance(override, int) and override > 0:
        return override
    caps = [cfg.get('generation_kwargs', {}).get('max_gen_toks')
            for cfg in raw.get('configs', {}).values() if isinstance(cfg, dict)]
    caps = [c for c in caps if isinstance(c, int) and c > 0]
    return max(caps) if caps else BACKEND_DEFAULT_CAP


def extracted(row):
    """True when the filter produced a usable answer; missing filtered output is an error."""
    if 'filtered_resps' not in row:
        raise ValueError('scored sample lacks filtered_resps; cannot measure extraction')
    value = row['filtered_resps']
    while isinstance(value, list):
        if not value:
            return False
        value = value[0]
    if not isinstance(value, str):
        return value is not None
    return bool(value.strip()) and value.strip() != INVALID


def answer_statistics(task_dir, children, cap):
    task_dir = Path(task_dir)
    responses = read_jsonl(task_dir / 'responses.jsonl')
    if not responses:
        raise ValueError('no responses to measure')
    tokens, blank = [], 0
    for response in responses:
        usage = response.get('usage')
        count = usage.get('completion_tokens') if isinstance(usage, dict) else None
        if type(count) is not int or count < 0:
            raise ValueError('response lacks usage.completion_tokens; cannot measure answer length')
        tokens.append(count)
        choice = response['choices'][0]
        content = (choice.get('message') or {}).get('content')
        if choice.get('finish_reason') == 'length' and not (content or '').strip():
            blank += 1
    filters = {}
    for child in children:
        for row in read_jsonl(task_dir / f'samples_{child}.jsonl'):
            entry = filters.setdefault(row.get('filter', 'none'), {'extracted': 0, 'answers': 0})
            entry['answers'] += 1
            entry['extracted'] += int(extracted(row))
    if not filters:
        raise ValueError('no scored samples to measure')
    for entry in filters.values():
        entry['share'] = entry['extracted'] / entry['answers']
    p90 = nearest_rank(tokens, 0.9)
    return {
        'answers': len(responses),
        'extraction': dict(sorted(filters.items())),
        'extraction_target': EXTRACTION_TARGET,
        'blank_at_cap': blank,
        'blank_at_cap_share': blank / len(responses),
        'blank_at_cap_limit': BLANK_AT_CAP_LIMIT,
        'answer_tokens': {'median': nearest_rank(tokens, 0.5), 'p90': p90, 'max': max(tokens)},
        'output_cap': cap,
        'recommended_output_cap': recommended_cap(p90, cap),
    }


def advisories(task, stats):
    lines = []
    for name, entry in stats['extraction'].items():
        if entry['share'] < stats['extraction_target']:
            lines.append(f"{task}: filter {name} extracted {entry['extracted']}/{entry['answers']} answers "
                         f"({100 * entry['share']:.1f}% < {100 * stats['extraction_target']:.0f}%); "
                         'check answer format, few-shot/template and scorer regex')
    if stats['blank_at_cap_share'] > stats['blank_at_cap_limit']:
        lines.append(f"{task}: {stats['blank_at_cap']}/{stats['answers']} answers blank at the output cap "
                     f"{stats['output_cap']} ({100 * stats['blank_at_cap_share']:.1f}% > "
                     f"{100 * stats['blank_at_cap_limit']:.0f}%); raise the cap toward "
                     f"{stats['recommended_output_cap']} or reduce samples")
    return lines


def task_statistics(run_dir, config, manifest, task):
    run_dir = Path(run_dir)
    raw = json.loads((run_dir / task / 'results.json').read_text())
    return answer_statistics(run_dir / task, manifest['groups'][task]['tasks'], output_cap(config, task, raw))
