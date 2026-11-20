"""Goal 6b gate: a frozen reference corpus, both comparisons, a match at full precision and the tiers."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/scripts/check_reference_accuracy.py'
KINDS = ('reasoning', 'agentic', 'long', 'stress', 'on_policy', 'held_out')


def record():
    numbers = {k: {'kl_mean': 0.01, 'kl_p95': 0.05, 'top1': 0.97} for k in KINDS}
    return {
        'reference': {'model': 'org/model', 'revision': 'abc', 'dtype': 'bf16', 'device': 'cpu'},
        'slices': [{'name': k, 'kind': k, 'positions': 2048, 'tokens_sha256': 'a' * 64, 'logits_sha256': 'b' * 64}
                   for k in KINDS],
        'full': {'meaning': 'bf16_hifi4', 'unreached_ops': [], 'per_slice': numbers},
        'current': {'precision': 'bfp8_all_lofi', 'per_slice': numbers},
        'tolerance': {'kl_p95': 0.01, 'top1': 0.01, 'source': 'HF bf16 vs fp32 on 512 positions'},
        'verdict': 'match', 'divergences': [],
        'tiers': {t: {'command': f'python -m reference_check --tier {t}'} for t in ('quick', 'full', 'long', 'served')},
        'baseline': {'precision': 'bfp8_all_lofi', 'per_slice': numbers},
    }


def run(tmp_path, data):
    path = tmp_path / 'm/doc/reference/reference.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_text(json.dumps(data))
    result = subprocess.run([sys.executable, str(CHECK), '--model-dir', str(tmp_path / 'm')],
                            capture_output=True, text=True)
    return result.returncode, result.stderr


def test_complete_record_passes(tmp_path):
    assert run(tmp_path, record()) == (0, '')


def test_missing_record_is_critical(tmp_path):
    code, stderr = run(tmp_path, None)
    assert code == 2 and 'missing' in stderr


def test_missing_slice_or_comparison_is_critical(tmp_path):
    data = record()
    data['slices'] = [s for s in data['slices'] if s['kind'] != 'on_policy']
    assert run(tmp_path, data)[0] == 2
    data = record()
    del data['full']['per_slice']['long']
    code, stderr = run(tmp_path, data)
    assert code == 2 and "full: slice 'long'" in stderr


def test_open_divergence_fails_and_a_fixed_one_passes(tmp_path):
    data = record()
    data['divergences'] = [{'where': 'layer 3 rope', 'cause': 'scaling', 'fix': '', 'status': 'open'}]
    assert run(tmp_path, data)[0] == 2
    data['divergences'][0].update(fix='use yarn factor', status='fixed')
    assert run(tmp_path, data)[0] == 0
    data['verdict'] = 'divergence'
    assert run(tmp_path, data)[0] == 2


def test_missing_tier_or_baseline_is_advisory(tmp_path):
    data = record()
    del data['tiers']['served']
    data['baseline'] = {}
    code, stderr = run(tmp_path, data)
    assert code == 1 and "tier 'served'" in stderr and 'baseline' in stderr
