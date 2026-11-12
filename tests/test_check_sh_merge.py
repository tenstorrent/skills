"""Stage gate scripts must run every sub-check; the worst exit code wins."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
PROMPTS = PLUGIN / 'prompts/model_bringup_multigoal'


def shim_env(tmp_path: Path, codes: dict[str, int]) -> dict[str, str]:
    """Put a fake `python` first on PATH: named checkers exit with fixed codes, the rest run for real."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    cases = ''.join(f'    *{name}*) echo "fake {name}" >&2; exit {code};;\n' for name, code in codes.items())
    shim = bin_dir / 'python'
    shim.write_text(f'#!/usr/bin/env bash\ncase "$*" in\n{cases}esac\nexec {sys.executable} "$@"\n')
    shim.chmod(0o755)
    return dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')


def gate(stage: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    model = tmp_path / 'models/autoports/org_model'
    model.mkdir(parents=True, exist_ok=True)
    env = dict(env, MODEL_DIR=str(model))
    return subprocess.run(['bash', str(PROMPTS / f'{stage}.check.sh')], cwd=tmp_path, env=env,
                          text=True, capture_output=True)


def test_stage9_advisory_degenerate_output_still_runs_eval_readiness(tmp_path):
    env = shim_env(tmp_path, {'check_degenerate_output.py': 1, 'check_context_contract.py': 0,
                              'check_serving_contract.py': 0, 'check_prefill_features.py': 0})
    result = gate('09-vllm', tmp_path, env)
    # The probe results are missing, so the eval-readiness gate must run and fail critical.
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'no result' in result.stdout + result.stderr


@pytest.mark.parametrize('stage', ['06-full-model', '09-vllm', '10-optimized-vllm'])
def test_advisory_first_check_does_not_hide_later_critical(tmp_path, stage):
    codes = {'check_degenerate_output.py': 1, 'check_context_contract.py': 1, 'check_serving_contract.py': 1,
             'check_prefill_features.py': 1, 'check_eval_readiness.py': 1}
    last = {'06-full-model': 'check_serving_contract.py', '09-vllm': 'check_eval_readiness.py',
            '10-optimized-vllm': 'check_eval_readiness.py'}[stage]
    assert gate(stage, tmp_path, shim_env(tmp_path, codes)).returncode == 1
    (tmp_path / 'bin/python').unlink()
    (tmp_path / 'bin').rmdir()
    codes[last] = 2
    result = gate(stage, tmp_path, shim_env(tmp_path, codes))
    assert result.returncode == 2, result.stdout + result.stderr
    assert f'fake {last}' in result.stderr


@pytest.mark.parametrize('stage', ['06-full-model', '07-optimized-full-model', '09-vllm', '10-optimized-vllm'])
def test_checker_error_is_exit_3(tmp_path, stage):
    result = gate(stage, tmp_path, shim_env(tmp_path, {'check_degenerate_output.py': 3}))
    assert result.returncode == 3, result.stdout + result.stderr
