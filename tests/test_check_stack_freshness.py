"""Exit-code contract and freshness arithmetic for the stack freshness check."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / 'plugins/tt-model-bringup/scripts/check_stack_freshness.py'


def git(repo: Path, *args: str, env=None) -> str:
    base = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
    base.update(env or {})
    full = os.environ.copy()
    full.update(base)
    return subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True, check=True, env=full).stdout.strip()


def commit(repo: Path, name: str, when: str) -> str:
    (repo / name).write_text(name)
    git(repo, 'add', name)
    git(repo, 'commit', '-q', '-m', name, env={'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when})
    return git(repo, 'rev-parse', 'HEAD')


def make_pair(tmp_path: Path, name: str, *, upstream_days_ahead: int) -> tuple[Path, Path]:
    upstream = tmp_path / f'{name}-upstream'
    upstream.mkdir()
    git(upstream, 'init', '-q', '-b', 'main')
    commit(upstream, 'base', '2026-10-01T00:00:00Z')
    clone = tmp_path / f'{name}-clone'
    git(tmp_path, 'clone', '-q', str(upstream), str(clone))
    git(clone, 'checkout', '-q', '-b', 'work')
    if upstream_days_ahead:
        day = 1 + upstream_days_ahead
        commit(upstream, 'newer', f'2026-10-{day:02d}T00:00:00Z')
    return upstream, clone


def run(*args: str, cwd: Path, env=None):
    full = os.environ.copy()
    full.update(env or {})
    return subprocess.run([sys.executable, str(CHECKER), *args], cwd=cwd, text=True, capture_output=True, env=full)


def plugin_json(tmp_path: Path, version: str) -> Path:
    path = tmp_path / f'latest-{version}.json'
    path.write_text(json.dumps({'name': 'tt-model-bringup', 'version': version}))
    return path


def installed_version() -> str:
    return json.loads((REPO / 'plugins/tt-model-bringup/.claude-plugin/plugin.json').read_text())['version']


def resolve_args(tmp_path: Path, metal: Path, plugin: Path, *, latest: str | None = None, extra=()):
    args = ['--mode', 'resolve', '--model-dir', str(tmp_path / 'model'), '--repo', str(metal),
            '--plugin-root', str(plugin), '--transformers-version', '5.12.1',
            '--hf-config', str(tmp_path / 'config.json'), '--max-age-days', '7']
    (tmp_path / 'config.json').write_text(json.dumps({'transformers_version': '4.51.0'}))
    args += ['--skills-latest-file', str(plugin_json(tmp_path, latest or installed_version()))]
    return args + list(extra)


def test_fresh_stack_passes_and_records_pins(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=0)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=2)
    result = run(*resolve_args(tmp_path, metal, plugin), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    record = json.loads((tmp_path / 'model/doc/stack_pins.json').read_text())
    assert record['verdict'] == 'pass'
    assert record['tt_metal']['head'] == git(metal, 'rev-parse', 'HEAD')
    assert record['vllm_tt_plugin']['commits_behind'] == 1
    assert record['vllm_tt_plugin']['verdict'] == 'fresh'
    assert record['transformers']['verdict'] == 'ok'
    assert record['skills_plugin']['verdict'] == 'fresh'


def test_stale_tt_metal_is_critical_for_a_new_run_and_advisory_on_resume(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=12)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=0)
    result = run(*resolve_args(tmp_path, metal, plugin), cwd=tmp_path)
    assert result.returncode == 2
    assert 'tt_metal is 12.0 days and 1 commits behind' in result.stderr
    resumed = run(*resolve_args(tmp_path, metal, plugin, extra=['--resume']), cwd=tmp_path)
    assert resumed.returncode == 1, resumed.stderr


def test_transformers_below_checkpoint_minimum_is_critical(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=0)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=0)
    args = resolve_args(tmp_path, metal, plugin)
    args[args.index('5.12.1')] = '4.40.0'
    result = run(*args, cwd=tmp_path)
    assert result.returncode == 2
    assert 'below the checkpoint' in result.stderr


def test_stale_skills_plugin_stops_a_new_run(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=0)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=0)
    result = run(*resolve_args(tmp_path, metal, plugin, latest='99.0.0'), cwd=tmp_path)
    assert result.returncode == 2
    assert 'update it with the host' in result.stderr


def test_offline_resolution_is_advisory_not_fatal(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=0)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=0)
    args = resolve_args(tmp_path, metal, plugin)
    args = [a for a in args if not a.startswith('--skills-latest-file') and not a.endswith('.json') or a.endswith('config.json')]
    result = run(*args, '--offline', cwd=tmp_path)
    assert result.returncode == 1, result.stderr
    assert 'could not be verified' in result.stderr


def test_verify_detects_a_rewound_checkout_and_a_contract_mismatch(tmp_path):
    _, metal = make_pair(tmp_path, 'metal', upstream_days_ahead=0)
    _, plugin = make_pair(tmp_path, 'plugin', upstream_days_ahead=0)
    assert run(*resolve_args(tmp_path, metal, plugin), cwd=tmp_path).returncode == 0
    verify = ['--mode', 'verify', '--model-dir', str(tmp_path / 'model'), '--repo', str(metal), '--plugin-root', str(plugin)]
    assert run(*verify, cwd=tmp_path).returncode == 0
    commit(metal, 'stage-work', '2026-10-03T00:00:00Z')
    ahead = run(*verify, cwd=tmp_path)
    assert ahead.returncode == 1
    assert 'commits past the recorded pin' in ahead.stderr
    contract = tmp_path / 'serving_contract.json'
    contract.write_text(json.dumps({'plugin': {'commit': 'f' * 40}}))
    mismatch = run(*verify, '--serving-contract', str(contract), cwd=tmp_path)
    assert mismatch.returncode == 2
    assert 'serving contract pins plugin' in mismatch.stderr
    git(metal, 'checkout', '-q', '--orphan', 'elsewhere')
    commit(metal, 'unrelated', '2026-10-04T00:00:00Z')
    rewound = run(*verify, cwd=tmp_path)
    assert rewound.returncode == 2
    assert 'does not descend from it' in rewound.stderr


def test_verify_without_record_is_critical_and_bad_args_are_errors(tmp_path):
    result = run('--mode', 'verify', '--model-dir', str(tmp_path / 'model'), cwd=tmp_path)
    assert result.returncode == 2
    assert 'missing' in result.stderr
    assert run('--mode', 'bogus', '--model-dir', 'x', cwd=tmp_path).returncode == 3
    assert run('--mode', 'verify', cwd=tmp_path).returncode == 3
