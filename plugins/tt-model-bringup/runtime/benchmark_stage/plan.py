"""Shared stage plan: batch profiles, lone-user rows and the two clocks.

The runner, checker and report read the same plan so they agree on which
profiles exist and which budget applies. Every number here is a default the
owner can tune through the run configuration.
"""
from __future__ import annotations

DEFAULT_BATCH_SIZES = (1, 8, 16, 32)
# Client clock: accuracy, measured requests, phase collection and reporting.
CLIENT_SECONDS_PER_BATCH = 1800
# Setup clock: server launches/reloads, compilation and warmups. Own bound.
DEFAULT_SETUP_SECONDS_PER_PROFILE = 1800
INPUT_TOKENS = 4096
LONG_CONTEXT_INPUT_TOKENS = 32768
# Lone-user rows on the largest server: (name, input tokens or None for long, requests).
FUNCTIONAL_PROFILE = 'functional'
LONG_CONTEXT_PROFILE = 'long_context'
LONE_USER_REQUESTS = {FUNCTIONAL_PROFILE: 4, LONG_CONTEXT_PROFILE: 2}
FUNCTIONAL_INPUT_TOKENS = 128


def positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def perf_requests(batch):
    return max(8, batch * 3)


def batch_plan(config):
    """Resolve configured, skipped and measured batch sizes plus both budgets."""
    sizes = config.get('batch_sizes', list(DEFAULT_BATCH_SIZES))
    if (not isinstance(sizes, list) or not sizes or not all(positive_int(b) for b in sizes)
            or len(set(sizes)) != len(sizes)):
        raise ValueError('batch_sizes must be a non-empty list of unique positive integers')
    skipped = config.get('skipped_batch_sizes', {})
    if not isinstance(skipped, dict) or not all(
            isinstance(k, str) and k.isdigit() and int(k) in sizes and isinstance(v, str) and v.strip()
            for k, v in skipped.items()):
        raise ValueError('skipped_batch_sizes maps a configured batch size (string key) to a non-empty reason')
    measured = sorted((b for b in sizes if str(b) not in skipped), reverse=True)
    if not measured:
        raise ValueError('every configured batch size is skipped; nothing to measure')
    cap = float(CLIENT_SECONDS_PER_BATCH * len(sizes))
    client_budget = float(config.get('budget_seconds', cap))
    if not 0 < client_budget <= cap:
        raise ValueError(f'budget_seconds (client clock) must be positive and at most {cap:.0f}')
    setup_budget = float(config.get('setup_budget_seconds', DEFAULT_SETUP_SECONDS_PER_PROFILE * len(measured)))
    if not setup_budget > 0:
        raise ValueError('setup_budget_seconds must be positive')
    return {
        'batch_sizes': sorted(sizes, reverse=True),
        'skipped_batch_sizes': dict(skipped),
        'measured_batch_sizes': measured,
        'accuracy_concurrency': measured[0],
        'client_budget_seconds': client_budget,
        'setup_budget_seconds': setup_budget,
    }


def lone_user_profiles(largest, max_model_len, output_tokens, long_input=LONG_CONTEXT_INPUT_TOKENS):
    """Concurrency-1 rows on the largest server: FUNCTIONAL reference and long context."""
    long_tokens = min(long_input, max_model_len - output_tokens)
    return [
        {'name': FUNCTIONAL_PROFILE, 'server_batch': largest, 'concurrency': 1,
         'input_tokens': FUNCTIONAL_INPUT_TOKENS, 'output_tokens': output_tokens,
         'requests': LONE_USER_REQUESTS[FUNCTIONAL_PROFILE],
         'file': f'perf-b{largest}-c1-{FUNCTIONAL_PROFILE}'},
        {'name': LONG_CONTEXT_PROFILE, 'server_batch': largest, 'concurrency': 1,
         'input_tokens': long_tokens, 'output_tokens': output_tokens,
         'requests': LONE_USER_REQUESTS[LONG_CONTEXT_PROFILE],
         'file': f'perf-b{largest}-c1-{LONG_CONTEXT_PROFILE}'},
    ]


def validate_sample_reduction(config, manifest=None):
    """A reduction records from/to counts and the measured-speed reason."""
    reduction = config.get('sample_reduction', {})
    if not isinstance(reduction, dict):
        raise ValueError('sample_reduction must map task to {from, to, reason}')
    for task, row in reduction.items():
        if task not in config.get('tasks', []):
            raise ValueError(f'sample_reduction names a task that is not run: {task}')
        if (not isinstance(row, dict) or not positive_int(row.get('from')) or not positive_int(row.get('to'))
                or row['to'] >= row['from'] or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError(f'{task}: sample_reduction needs from > to > 0 and a measured-speed reason')
        if manifest is not None and manifest['groups'][task]['sample_count'] != row['to']:
            raise ValueError(f'{task}: sample_reduction.to differs from the frozen subset size')
    return reduction
