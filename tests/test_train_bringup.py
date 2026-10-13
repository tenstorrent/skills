"""No-model packaging and goal-contract regressions for tt-train-bringup."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-train-bringup'
AUTODEBUG = REPO / 'plugins/tt-autodebug'
PROMPTS = PLUGIN / 'prompts/train_bringup_multigoal'


def _load_runner():
    # Sibling plugins share bare module names (environment, telemetry_hooks); load this
    # runner against its own scripts directory and restore whatever another test module cached.
    shared = ('environment', 'telemetry_hooks', 'telemetry_access')
    saved = {name: sys.modules.pop(name) for name in shared if name in sys.modules}
    sys.path.insert(0, str(PLUGIN / 'scripts'))
    try:
        loader = importlib.machinery.SourceFileLoader('train_bringup_runner_contracts', str(PLUGIN / 'scripts/multigoal'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
    finally:
        sys.path.remove(str(PLUGIN / 'scripts'))
        for name in shared:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
    return module


runner = _load_runner()

REPLACEMENTS = ['--replace', 'HF_MODEL=org/model',
                '--replace', 'MODEL_DIR=tt-train/sources/ttml/ttml/models/org_model',
                '--replace', 'RECIPE=full fine-tuning, seq 2048, batch 1, AdamW']


def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / 'clean installation'
    bringup = root / 'tt-train-bringup'
    debug = root / 'tt-autodebug'
    shutil.copytree(PLUGIN, bringup, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(AUTODEBUG, debug, ignore=shutil.ignore_patterns('__pycache__'))
    target = tmp_path / 'target checkout'
    target.mkdir()
    env = os.environ.copy()
    env.update(TT_AUTODEBUG_ROOT=str(debug), CODEX_HOME=str(tmp_path / 'codex-home'))
    return bringup, target, env


def test_three_phases_only():
    assert [p.name for p in sorted(PROMPTS.glob('*.txt'))] == [
        '01-plan.txt', '02-composite-bringup.txt', '03-optimization-loop.txt']
    for path in PROMPTS.glob('*.txt'):
        assert 'multichip' not in path.read_text().lower(), path
    for path in (PLUGIN / 'skills').rglob('SKILL.md'):
        assert 'multichip' not in path.read_text().lower(), path


def test_missing_dependency_stops_before_launch_or_artifacts(installed):
    plugin, target, env = installed
    env.pop('TT_AUTODEBUG_ROOT', None)
    result = run(sys.executable, str(plugin / 'scripts/multigoal'),
                 str(plugin / 'prompts/train_bringup_multigoal/01-plan.txt'),
                 '--dry-run', cwd=target, env=env)
    assert result.returncode != 0
    assert 'codex plugin add tt-autodebug@tenstorrent-skills' in result.stderr
    assert '/plugin install tt-autodebug@tenstorrent-skills' in result.stderr
    assert not list(target.iterdir())


def test_clean_package_runs_all_goals_dry(installed):
    plugin, target, env = installed
    prompts = sorted((plugin / 'prompts/train_bringup_multigoal').glob('*.txt'))
    result = run(sys.executable, str(plugin / 'scripts/multigoal'), *map(str, prompts),
                 *REPLACEMENTS, '--dry-run', cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert 'missing skill' not in result.stderr
    manifests = list(target.glob('bringup/artifacts/multigoal-runs/*/manifest.txt'))
    assert len(manifests) == 1
    manifest = runner.read_manifest(manifests[0])
    assert manifest['stage_3_dry_run'] == 'true'
    assert len(list(manifests[0].parent.glob('*.prompt.txt'))) == 3
    assert not (target / '.agents').exists()


def test_goal_skill_references_and_objectives(monkeypatch):
    monkeypatch.setenv('TT_AUTODEBUG_ROOT', str(AUTODEBUG))
    for path in PROMPTS.glob('*.txt'):
        objective = runner.objective_from_prompt(runner.load_prompt(path, [('HF_MODEL', 'org/model')]))
        items, missing = runner.input_items_for_objective(Path('/unused'), objective)
        assert not missing, (path, missing)
        assert items[0]['text'] == objective
        for item in items[1:]:
            assert Path(item['path']).is_file()
            assert str(REPO / 'plugins') in item['path']


def test_skill_graph_and_package_links_resolve():
    names = {p.parent.name for p in (PLUGIN / 'skills').glob('*/SKILL.md')} | {'autodebug', 'autotriage', 'autofix'}
    assert {'train-bringup', 'train-plan', 'composite-bringup', 'optimization-loop'} <= names
    for path in (PLUGIN / 'skills').rglob('*.md'):
        text = path.read_text()
        assert set(re.findall(r'\$([a-z][a-z_-]+)', text)) <= names, path
        for ref in re.findall(r'\]\(([^)]+)\)', text):
            if '://' in ref or ref.startswith('#'):
                continue
            target = (path.parent / ref.split('#')[0]).resolve()
            assert target.is_relative_to(PLUGIN.resolve()), (path, ref)
            assert target.exists(), (path, ref)


def test_no_host_paths_in_package():
    for path in PLUGIN.rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        assert not path.is_symlink()
        if path.suffix in {'.md', '.py', '.txt', '.sh', '.json', '.yaml'}:
            assert not re.search(r'/Users/|/home/[^<\s]+|wh-lb-\d+', path.read_text()), path
