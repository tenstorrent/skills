"""The runner starts goal 12 over an empty doc/agentic_evals unless told to keep an earlier stage's outputs."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
from pathlib import Path
import sys

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
sys.path.insert(0, str(PLUGIN / 'scripts'))
loader = importlib.machinery.SourceFileLoader('multigoal_independent_run', str(PLUGIN / 'scripts/multigoal'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = runner
loader.exec_module(runner)


def test_agentic_evals_starts_empty_unless_the_operator_keeps_outputs(tmp_path):
    evals = tmp_path / 'm/doc/agentic_evals'
    evals.mkdir(parents=True)
    repl = [('MODEL_DIR', 'm')]
    fresh, keep = argparse.Namespace(keep_stage_outputs=False), argparse.Namespace(keep_stage_outputs=True)
    runner.require_independent_stage(fresh, tmp_path, repl, '12-agentic-evals')
    (evals / 'results.json').write_text('{}')
    runner.require_independent_stage(fresh, tmp_path, repl, '11-benchmark')
    runner.require_independent_stage(keep, tmp_path, repl, '12-agentic-evals')
    with pytest.raises(SystemExit, match='earlier run'):
        runner.require_independent_stage(fresh, tmp_path, repl, '12-agentic-evals')
