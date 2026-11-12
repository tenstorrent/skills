"""Gate checkers print at most ~40 lines and park long finding lists in a file; exit codes unchanged."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-model-bringup'
MAX_LINES = 41


def run(args, tmp_path, cwd=None):
    env = os.environ.copy()
    env.update(MULTIGOAL_LOG_DIR=str(tmp_path / 'logs'), MULTIGOAL_STAGE='06-full-model',
               PYTHONPATH=str(PLUGIN / 'runtime'))
    return subprocess.run([sys.executable, *args], cwd=cwd or tmp_path, env=env, text=True, capture_output=True)


def output_lines(result):
    return (result.stdout + result.stderr).splitlines()


def test_serving_contract_advisories_are_capped(tmp_path):
    model = tmp_path / 'models/autoports/org_model'
    (model / 'doc/full_model').mkdir(parents=True)
    rows = [{'item': f'feature_{i}', 'kind': 'request_feature', 'status': 'supported',
             'evidence': f'doc/full_model/missing_{i}.log'} for i in range(60)]
    rows.append({'item': 'supports_prefix_caching', 'kind': 'capability', 'status': 'not_supported',
                 'declaration': 'documented default',
                 'reason': 'recurrent_state: the GDN state at start_pos is not recoverable from KV blocks'})
    contract = {'plugin': {'repository': 'tenstorrent/vllm-tt-plugin', 'commit': 'a' * 40},
                'vllm': {'version': 'v0.0.0'}, 'documents_read': ['docs/MODEL_INTERFACE.md'], 'rows': rows}
    (model / 'doc/full_model/serving_contract.json').write_text(json.dumps(contract))
    result = run([str(PLUGIN / 'scripts/check_serving_contract.py'), '--model-dir', str(model)], tmp_path)
    assert result.returncode == 1, result.stderr
    lines = output_lines(result)
    assert len(lines) <= MAX_LINES
    assert '... 30 more in' in result.stderr
    full = tmp_path / 'logs/06-full-model.serving_contract_advisories.full.txt'
    assert len(full.read_text().splitlines()) == 60


def test_context_contract_caps_are_capped(tmp_path):
    model = tmp_path / 'models/autoports/org_model'
    (model / 'doc').mkdir(parents=True)
    (model / 'doc/context_contract.json').write_text(
        json.dumps({'hf_advertised_context': 8192, 'current_supported_context': 8192}))
    (model / 'doc/benchmark').mkdir()
    (model / 'doc/benchmark/runs.json').write_text(json.dumps([{'max_model_len': 1024 + i} for i in range(50)]))
    result = run([str(PLUGIN / 'scripts/check_context_contract.py'), '--model-dir', str(model)], tmp_path)
    assert result.returncode == 2, result.stderr
    assert len(output_lines(result)) <= MAX_LINES
    assert '... 20 more in' in result.stderr
    assert (tmp_path / 'logs/06-full-model.context_contract_caps.full.txt').exists()


def test_degenerate_output_findings_are_capped(tmp_path):
    root = tmp_path / 'models/autoports'
    for i in range(20):
        doc = root / f'm{i}/doc/autoregressive'
        doc.mkdir(parents=True)
        (doc / 'autoregressive_meta.json').write_text(json.dumps({'tt': {'token_ids': [5] * 80}}))
        (doc / 'tt_completion.txt').write_text(' '.join(['loop'] * 80))
    report = tmp_path / 'report.json'
    result = run(['-m', 'readiness_check.check_degenerate_output', str(root), '--json', str(report)], tmp_path)
    assert result.returncode == 2, result.stderr
    assert len(output_lines(result)) <= MAX_LINES
    assert f'more in {report}' in result.stdout
    assert len(json.loads(report.read_text())['findings']) >= 20
    # Without --json the full list goes to a side file.
    result = run(['-m', 'readiness_check.check_degenerate_output', str(root)], tmp_path)
    assert result.returncode == 2
    assert (tmp_path / 'logs/06-full-model.degenerate_output_findings.full.txt').exists()


def test_short_lists_are_unchanged(tmp_path):
    model = tmp_path / 'models/autoports/org_model'
    (model / 'doc').mkdir(parents=True)
    (model / 'doc/context_contract.json').write_text(
        json.dumps({'hf_advertised_context': 8192, 'current_supported_context': 8192}))
    (model / 'doc/benchmark').mkdir()
    (model / 'doc/benchmark/runs.json').write_text(json.dumps([{'max_model_len': 1024}]))
    result = run([str(PLUGIN / 'scripts/check_context_contract.py'), '--model-dir', str(model)], tmp_path)
    assert result.returncode == 2
    assert 'more in' not in result.stderr
    assert 'CONTEXT CAP: doc/benchmark/runs.json' in result.stderr
