"""Scope gate: release blocks changes outside the model folder, open records them."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/scripts/check_scope.py'
MODEL = 'models/autoports/org_model'


def sh(repo, *args):
    subprocess.run(args, cwd=repo, check=True, capture_output=True)


def repo_with_change(tmp_path, scope, outside=True, base=True):
    repo = tmp_path / 'r'
    (repo / MODEL / 'bringup/agentic_evals').mkdir(parents=True)
    (repo / 'ttnn').mkdir()
    (repo / 'ttnn/op.cpp').write_text('a\n')
    (repo / MODEL / 'model.py').write_text('x\n')
    sh(repo, 'git', 'init', '-q')
    sh(repo, 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A')
    sh(repo, 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True).stdout.strip()
    inputs = {'scope': scope}
    if base:
        inputs['base_commit'] = head
    (repo / MODEL / 'bringup/agentic_evals/inputs.json').write_text(json.dumps(inputs))
    (repo / MODEL / 'model.py').write_text('y\n')
    if outside:
        (repo / 'ttnn/op.cpp').write_text('b\n')
    return repo


def run(repo):
    r = subprocess.run([sys.executable, str(CHECK), '--model-dir', MODEL], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stderr


def test_release_blocks_outside_changes(tmp_path):
    code, err = run(repo_with_change(tmp_path, 'release'))
    assert code == 2 and 'ttnn/op.cpp' in err


def test_release_allows_model_only_changes(tmp_path):
    assert run(repo_with_change(tmp_path, 'release', outside=False))[0] == 0


def test_open_records_outside_changes(tmp_path):
    code, err = run(repo_with_change(tmp_path, 'open'))
    assert code == 0 and 'ttnn/op.cpp' in err


def test_missing_base_is_advisory_and_unknown_scope_critical(tmp_path):
    assert run(repo_with_change(tmp_path, 'release', outside=False, base=False))[0] == 1
    assert run(repo_with_change(tmp_path / 'x', 'loose'))[0] == 2
