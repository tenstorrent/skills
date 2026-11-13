"""explain_hours.py: the run replayed on stage 11's rates; interpolated, extrapolated beyond the grid and labelled."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/scripts'))
import explain_hours as eh  # noqa: E402


def row(batch, length, prefill, decode):
    return {'batch': batch, 'input_tokens': length, 'prefill_tok_s': prefill, 'decode_tok_s_user': decode,
            'ttft_s': 1, 'step_ms': 1}


RATES = {'validated_batches': [1, 8], 'rows': [
    row(1, 4096, 1000, 10), row(1, 32768, 500, 5),
    row(8, 4096, 800, 8), row(8, 32768, 400, 4)]}


def call(start, end, uncached, output, context):
    return {'start': start, 'end': end, 'uncached_input': uncached, 'output': output, 'context': context}


def test_one_call_inside_the_grid_is_exact():
    result = eh.explain(RATES, {'agents': 1, 'tasks': [{'task': 'a', 'calls': [call(0, 5, 2000, 100, 2100)]}]})
    assert result['explained_hours'] * 3600 == pytest.approx(2.0 + 10.0, abs=0.5)
    assert result['approximate'] is False and result['reasons'] == []


def test_rates_between_rows_are_interpolated_not_priced_at_the_next_row_up():
    # prompt 18432 is halfway between 4096 and 32768: prefill s/token halfway between 1/1000 and 1/500
    result = eh.explain(RATES, {'agents': 1, 'tasks': [{'task': 'a', 'calls': [call(0, 5, 18432, 0, 18432)]}]})
    assert result['tasks'][0]['server_s'] == pytest.approx(18432 * (0.001 + 0.002) / 2, abs=0.1)


def test_contexts_beyond_the_grid_are_extrapolated_and_marked_approximate():
    result = eh.explain(RATES, {'agents': 1, 'tasks': [{'task': 'a', 'calls': [call(0, 5, 0, 1000, 127000)]}]})
    assert result['approximate'] and 'extrapolated' in result['reasons'][0]
    assert result['tasks'][0]['server_s'] > 1000 / 5          # slower than the 32K row, never faster
    assert result['longest_context'] == 127000 and result['longest_row'] == 32768


def test_concurrency_picks_the_batch_and_tasks_are_scheduled_on_the_agents():
    tasks = [{'task': name, 'start': 0, 'end': 100, 'calls': [call(0, 100, 0, 80, 1000)]} for name in 'abc']
    result = eh.explain(RATES, {'agents': 2, 'tasks': tasks})
    per_task = 80 / 8                                          # three active: batch 8 at 8 tok/s/user
    assert all(t['server_s'] == pytest.approx(per_task) for t in result['tasks'])
    assert all(t['outside_s'] == pytest.approx(0) for t in result['tasks'])  # span 100 s minus the call's 100 s
    assert result['explained_hours'] * 3600 == pytest.approx(2 * per_task, abs=0.5)  # 3 tasks on 2 agents: two waves


def test_time_outside_the_server_is_added_from_the_task_span():
    task = {'task': 'swe', 'start': 0, 'end': 300, 'calls': [call(10, 20, 0, 10, 1000), call(50, 60, 0, 10, 1100)]}
    result = eh.explain(RATES, {'agents': 1, 'tasks': [task]})
    assert result['tasks'][0]['outside_s'] == pytest.approx(280)
    assert result['tasks'][0]['server_s'] == pytest.approx(2.0)


def test_a_batch_without_rows_uses_the_nearest_and_says_so():
    rates = {'validated_batches': [1, 32], 'rows': [row(1, 4096, 1000, 10)]}
    tasks = [{'task': str(i), 'calls': [call(0, 10, 0, 10, 100)]} for i in range(4)]
    result = eh.explain(rates, {'agents': 4, 'tasks': tasks})
    assert result['approximate'] and any('batch 32 has no rate rows' in r for r in result['reasons'])


def test_a_big_server_with_few_requests_in_flight_interpolates_between_concurrencies():
    rates = {'validated_batches': [32], 'rows': [
        row(32, 4096, 800, 4), {**row(32, 4096, 1000, 13), 'concurrency': 1}]}
    one = eh.explain(rates, {'agents': 1, 'server_batch': 32, 'tasks': [
        {'task': 'a', 'calls': [call(0, 10, 0, 130, 1000)]}]})
    assert one['tasks'][0]['server_s'] == pytest.approx(10.0, abs=0.1) and not one['approximate']
    # 17 in flight: halfway between 1 and 32 users in seconds per token
    tasks = [{'task': str(i), 'calls': [call(0, 10, 0, 100, 1000)]} for i in range(17)]
    many = eh.explain(rates, {'agents': 17, 'server_batch': 32, 'tasks': tasks})
    halfway = 100 * (1 / 13 + (1 / 4 - 1 / 13) * 16 / 31)
    assert many['tasks'][0]['server_s'] == pytest.approx(halfway, abs=0.1)
    full_only = eh.explain({'validated_batches': [32], 'rows': [row(32, 4096, 800, 4)]},
                           {'agents': 1, 'server_batch': 32, 'tasks': [{'task': 'a', 'calls': [call(0, 10, 0, 40, 1000)]}]})
    assert full_only['approximate'] and 'priced as a full batch' in full_only['reasons'][0]
