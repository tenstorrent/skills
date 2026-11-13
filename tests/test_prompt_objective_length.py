"""Every stage prompt fits the Codex goal-objective limit after HF_MODEL becomes a long, real model id.

google/gemma-4-31B-it took a 3,995-char stage-12 prompt to a 4,001-char objective and the runner refused to start.
Model ids of 40 characters are common, so each objective is measured with one, through the runner's own code.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
LOADER = importlib.machinery.SourceFileLoader('multigoal_for_prompt_lengths', str(PLUGIN / 'scripts/multigoal'))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
MULTIGOAL = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = MULTIGOAL
LOADER.exec_module(MULTIGOAL)

LONG_ID = 'an-organisation/' + 'a-model-name-24-chars-xx'
PROMPTS = sorted((PLUGIN / 'prompts/model_bringup_multigoal').glob('*.txt'))


def test_the_id_is_40_chars_and_there_are_prompts():
    assert len(LONG_ID) == 40 and len(PROMPTS) >= 12


@pytest.mark.parametrize('path', PROMPTS, ids=lambda p: p.name)
def test_prompt_objective_fits_with_a_40_char_model_id(path):
    text = MULTIGOAL.load_prompt(path, [('HF_MODEL', LONG_ID), ('MODEL_DIR', 'models/autoports/x')])
    objective = MULTIGOAL.objective_from_prompt(text)   # SystemExit when over the limit
    assert len(objective) <= MULTIGOAL.MAX_GOAL_OBJECTIVE_CHARS
