"""Lean pipeline: two outcome-defined goals (model, serve) plus goal 12; prompts fit, skills exist, gates work."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
LEAN = PLUGIN / 'prompts/model_bringup_lean'
LOADER = importlib.machinery.SourceFileLoader('multigoal_for_lean', str(PLUGIN / 'scripts/multigoal'))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
MULTIGOAL = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = MULTIGOAL
LOADER.exec_module(MULTIGOAL)
PROMPTS = sorted(LEAN.glob('*.txt'))
CHECK = PLUGIN / 'scripts/check_model_goal.py'


def test_lean_goals_exist_with_check_scripts():
    assert [p.name for p in PROMPTS] == ['1-model.txt', '2-serve.txt']
    for prompt in PROMPTS:
        assert prompt.with_name(prompt.stem + '.check.sh').is_file()


@pytest.mark.parametrize('path', PROMPTS, ids=lambda p: p.name)
def test_lean_prompt_fits_and_names_existing_skills(path):
    text = MULTIGOAL.load_prompt(path, [('HF_MODEL', 'an-organisation/' + 'a-model-name-24-chars-xx'),
                                        ('MODEL_DIR', 'models/autoports/x')])
    assert len(MULTIGOAL.objective_from_prompt(text)) <= MULTIGOAL.MAX_GOAL_OBJECTIVE_CHARS
    for name in set(re.findall(r'\$([A-Za-z0-9][A-Za-z0-9_-]*)', path.read_text())) - {'autofix'}:
        assert (PLUGIN / 'skills' / name / 'SKILL.md').is_file(), name


def record():
    return {'budget_hours': 24, 'started_at': '2026-11-21T08:00:00Z', 'hours_used': 23.5,
            'reset_authorization': 'bounded resets and health checks authorized on this box',
            'targets': {f'decode_ms_b{b}': 10.0 * (1 + b / 32) for b in (1, 8, 16, 32)},
            'floors': {f'decode_ms_b{b}': 5.0 * (1 + b / 32) for b in (1, 8, 16, 32)},
            'measured': {**{f'decode_ms_b{b}': 12.0 * (1 + b / 32) for b in (1, 8, 16, 32)},
                         'batched_equals_alone': True},
            'precision': 'tt/selected_precision_config.json',
            'perf_ledger': [{'change': 'all cores for decode matmuls', 'before': 30.0, 'after': 12.0, 'tier': 'T1 pass'}],
            'tiers': {t: {'command': f'pytest -m {t}'} for t in ('T0', 'T1', 'T2')}, 'moe': None, 'ops': []}


def run(tmp_path, data, inputs=None):
    model = tmp_path / 'm'
    (model / 'doc/model').mkdir(parents=True, exist_ok=True)
    if data is not None:
        (model / 'doc/model/model.json').write_text(json.dumps(data))
    if inputs is not None:
        (model / 'bringup/agentic_evals').mkdir(parents=True, exist_ok=True)
        (model / 'bringup/agentic_evals/inputs.json').write_text(json.dumps(inputs))
    result = subprocess.run([sys.executable, str(CHECK), '--model-dir', str(model)], capture_output=True, text=True)
    return result.returncode, result.stderr


def test_complete_record_passes(tmp_path):
    assert run(tmp_path, record()) == (0, '')


def test_missing_record_or_batch_or_equality_is_critical(tmp_path):
    assert run(tmp_path, None)[0] == 2
    data = record()
    del data['measured']['decode_ms_b16']
    assert run(tmp_path, data)[0] == 2
    data = record()
    data['measured']['batched_equals_alone'] = False
    assert run(tmp_path, data)[0] == 2
    data = record()
    data['perf_ledger'] = []
    assert run(tmp_path, data)[0] == 2


def test_batches_come_from_inputs(tmp_path):
    data = record()
    del data['measured']['decode_ms_b16']
    assert run(tmp_path, data, inputs={'batch_sizes': [1, 8, 32]}) == (0, '')


def test_far_from_target_needs_a_reason(tmp_path):
    data = record()
    data['measured']['decode_ms_b1'] = 30.0
    code, stderr = run(tmp_path, data)
    assert code == 1 and 'over 2x its target' in stderr
    data['reasons'] = {'decode_ms_b1': 'host n-gram lookup per step, measured 9 ms'}
    assert run(tmp_path, data) == (0, '')


def test_new_ops_need_a_test_and_a_switch(tmp_path):
    data = record()
    data['ops'] = [{'name': 'fused_moe_decode', 'kind': 'fusion', 'test': 'tests/test_fused_moe.py'}]
    code, stderr = run(tmp_path, data)
    assert code == 2 and 'switch' in stderr
    data['ops'][0]['switch'] = 'FUSED_MOE=0'
    assert run(tmp_path, data) == (0, '')


def test_moe_needs_per_batch_numbers(tmp_path):
    data = record()
    data['moe'] = {'experts': 128, 'top_k': 8, 'layout': 'expert-parallel, 32 per die'}
    code, stderr = run(tmp_path, data)
    assert code == 2 and 'moe.per_batch.b1' in stderr and 'moe.per_batch.b32' in stderr
    data['moe']['per_batch'] = {f'b{b}': {'distinct_experts': e, 'decode_ms': 9.0, 'floor_ms': 4.0}
                                for b, e in ((1, 8), (32, 110))}
    assert run(tmp_path, data) == (0, '')
    del data['moe']
    assert run(tmp_path, data)[0] == 1


def test_start_budget_and_reset_authorization_are_recorded(tmp_path):
    for key in ('started_at', 'reset_authorization'):
        data = record()
        del data[key]
        code, stderr = run(tmp_path, data)
        assert code == 2 and key in stderr
    code, stderr = run(tmp_path, record(), inputs={'budget_hours': 36})
    assert code == 2 and '36 h' in stderr
    data = record()
    data['budget_hours'] = 36
    assert run(tmp_path, data, inputs={'budget_hours': 36}) == (0, '')


def test_hours_well_over_budget_are_advisory(tmp_path):
    data = record()
    data['hours_used'] = 40
    code, stderr = run(tmp_path, data)
    assert code == 1 and 'over the 24 h budget' in stderr
