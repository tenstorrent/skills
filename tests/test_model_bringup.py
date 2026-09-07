"""No-model packaging, stage-contract and artifact gate regressions."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-model-bringup'
AUTODEBUG = REPO / 'plugins/tt-autodebug'
loader = importlib.machinery.SourceFileLoader('bringup_runner_contracts', str(PLUGIN / 'scripts/multigoal'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = runner
loader.exec_module(runner)


def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)


@pytest.fixture
def installed(tmp_path):
    # Copy only these two packages, with spaces in paths and no source framework.
    root = tmp_path / 'clean installation'
    bringup = root / 'tt-model-bringup'
    debug = root / 'tt-autodebug'
    shutil.copytree(PLUGIN, bringup, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(AUTODEBUG, debug, ignore=shutil.ignore_patterns('__pycache__'))
    target = tmp_path / 'target checkout'
    target.mkdir()
    env = os.environ.copy()
    env.update(TT_AUTODEBUG_ROOT=str(debug), CODEX_HOME=str(tmp_path / 'codex-home'))
    return bringup, target, env


def test_missing_dependency_stops_before_launch_or_artifacts(installed):
    plugin, target, env = installed
    env.pop('TT_AUTODEBUG_ROOT', None)
    result = run(sys.executable, str(plugin / 'scripts/multigoal'),
                 str(plugin / 'prompts/model_bringup_multigoal/01-functional-decoder.txt'),
                 '--dry-run', cwd=target, env=env)
    assert result.returncode != 0
    assert 'codex plugin add tt-autodebug@tenstorrent-skills' in result.stderr
    assert '/plugin install tt-autodebug@tenstorrent-skills' in result.stderr
    assert not list(target.iterdir())


def test_incomplete_dependency_is_rejected(installed):
    plugin, target, env = installed
    (Path(env['TT_AUTODEBUG_ROOT']) / 'skills/autofix/SKILL.md').unlink()
    result = run(sys.executable, str(plugin / 'scripts/environment.py'), cwd=target, env=env)
    assert result.returncode != 0
    assert 'Invalid tt-autodebug installation' in result.stderr
    assert not result.stdout


def test_clean_package_runs_all_goals_without_codex_or_old_framework(installed):
    plugin, target, env = installed
    prompts = sorted((plugin / 'prompts/model_bringup_multigoal').glob('*.txt'))
    result = run(sys.executable, str(plugin / 'scripts/multigoal'), *map(str, prompts),
                 '--replace', 'HF_MODEL=org/model', '--replace', 'MODEL_DIR=models/autoports/org_model',
                 '--dry-run', cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert 'missing skill' not in result.stderr
    manifests = list(target.glob('bringup/artifacts/multigoal-runs/*/manifest.txt'))
    assert len(manifests) == 1
    manifest = runner.read_manifest(manifests[0])
    assert manifest['stage_11_dry_run'] == 'true'
    assert manifest['stage_6_check_script'].startswith(str(plugin))
    assert len(list(manifests[0].parent.glob('*.prompt.txt'))) == 11
    assert not (target / '.agents').exists()


def test_environment_imports_offline_checker_without_model_dependencies(installed):
    plugin, target, env = installed
    result = run('bash', '-c',
                 'exports=$("$1" "$2/scripts/environment.py") || exit; eval "$exports"; '
                 '"$1" -c "import readiness_check.check_degenerate_output; '
                 'import sys; assert \'torch\' not in sys.modules"',
                 'test', sys.executable, str(plugin), cwd=target, env=env)
    assert result.returncode == 0, result.stderr


def test_goal_skill_references_and_objectives(monkeypatch):
    monkeypatch.setenv('TT_AUTODEBUG_ROOT', str(AUTODEBUG))
    for path in (PLUGIN / 'prompts/model_bringup_multigoal').glob('*.txt'):
        objective = runner.objective_from_prompt(runner.load_prompt(path, [('HF_MODEL', 'org/model')]))
        items, missing = runner.input_items_for_objective(Path('/unused'), objective)
        assert not missing
        assert items[0]['text'] == objective
        for item in items[1:]:
            assert Path(item['path']).is_file()
            assert str(REPO / 'plugins') in item['path']
    with pytest.raises(SystemExit, match='limit'):
        runner.objective_from_prompt('/goal ' + 'x' * 4001)


def test_skill_graph_and_package_links_resolve():
    names = {p.parent.name for p in (PLUGIN / 'skills').glob('*/SKILL.md')} | {'autodebug', 'autotriage', 'autofix'}
    for path in (PLUGIN / 'skills').rglob('*.md'):
        text = path.read_text()
        assert set(re.findall(r'\$([a-z][a-z_-]+)', text)) <= names, path
        for ref in re.findall(r'\]\(([^)]+)\)', text):
            if '://' in ref or ref.startswith('#'):
                continue
            target = (path.parent / ref.split('#')[0]).resolve()
            assert target.is_relative_to(PLUGIN.resolve()), (path, ref)
            assert target.exists(), (path, ref)


@pytest.mark.parametrize('stage', ['06-full-model', '07-optimized-full-model', '09-vllm', '10-optimized-vllm', '11-tti-release'])
def test_installed_gates_fail_critical_for_missing_evidence(installed, stage):
    plugin, target, env = installed
    env['MODEL_DIR'] = 'models/autoports/org_model'
    result = run('bash', str(plugin / 'prompts/model_bringup_multigoal' / (stage + '.check.sh')),
                 cwd=target, env=env)
    assert result.returncode == 2, result.stderr
    assert "can't open file" not in result.stderr


def test_full_model_gate_accepts_valid_evidence_and_rejects_context_reduction(installed):
    plugin, target, env = installed
    model = target / 'models/autoports/org_model'
    doc = model / 'doc'
    doc.mkdir(parents=True)
    # Artifact format recognized by the imported output scanner.
    output = doc / 'autoregressive'
    output.mkdir()
    (output / 'autoregressive_meta.json').write_text(json.dumps({'tt': {'token_ids': list(range(60))}}))
    (output / 'tt_completion.txt').write_text(' '.join(f'word{i}' for i in range(60)))
    contract = doc / 'context_contract.json'
    contract.write_text(json.dumps({'hf_advertised_context': 8192, 'current_supported_context': 8192}))
    env['MODEL_DIR'] = str(model)
    gate = plugin / 'prompts/model_bringup_multigoal/06-full-model.check.sh'
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    contract.write_text(json.dumps({'hf_advertised_context': 8192, 'current_supported_context': 1024}))
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 2
    assert 'without device-DRAM capacity evidence' in result.stderr


@pytest.mark.parametrize('codes, expected, calls', [([0], 'pass', 1), ([1], 'advisory-fail', 1),
    ([2], 'critical-fail', 1), ([3, 3], 'check-error(exit3)', 2), ([3, 0], 'pass', 2)])
def test_gate_exit_contract_and_infrastructure_retry(tmp_path, monkeypatch, codes, expected, calls):
    prompt = PLUGIN / 'prompts/model_bringup_multigoal/06-full-model.txt'
    sequence = iter(codes)
    seen = []
    def checker(*args):
        seen.append(args)
        return next(sequence)
    monkeypatch.setattr(runner, 'run_check_script', checker)
    result = runner.run_stage_checks(None, SimpleNamespace(no_checks=False, check_retries=0),
        tmp_path, tmp_path, tmp_path / 'manifest.txt', 6, 'full-model',
        prompt.read_text(), prompt, prompt, [('MODEL_DIR', 'models/autoports/org_model')])
    assert result == expected
    assert len(seen) == calls
    assert seen[0][2]['MODEL_DIR'] == 'models/autoports/org_model'


def test_model_scope_cannot_change_on_resume():
    with pytest.raises(SystemExit, match='does not match'):
        runner.resolve_model_dir_for_launch(replacements=[('MODEL_DIR', 'models/autoports/new')],
            manifest_values={'model_dir': 'models/autoports/original'}, required=True)


def test_live_inventory_requires_enabled_skills_at_selected_installation(monkeypatch):
    monkeypatch.setenv('TT_AUTODEBUG_ROOT', str(AUTODEBUG))
    paths = list((PLUGIN / 'skills').glob('*/SKILL.md')) + list((AUTODEBUG / 'skills').glob('*/SKILL.md'))
    skills = [{'name': runner.qualified_skill_name(p.parent.name), 'path': str(p), 'enabled': True} for p in paths]
    runner.verify_enabled_installations({'data': [{'skills': skills}]})
    skills[-1]['enabled'] = False
    with pytest.raises(RuntimeError, match='missing or disabled'):
        runner.verify_enabled_installations({'data': [{'skills': skills}]})
    skills[-1]['enabled'] = True
    skills[-1]['path'] = '/unselected/stale/cache/SKILL.md'
    with pytest.raises(RuntimeError, match='missing or disabled'):
        runner.verify_enabled_installations({'data': [{'skills': skills}]})


def test_import_provenance_and_publication_boundary():
    import hashlib
    data = json.loads((PLUGIN / 'sync-source.json').read_text())
    assert re.fullmatch('[0-9a-f]{40}', data['commit'])
    for entry in data['files']:
        path = (PLUGIN / entry['destination']).resolve()
        assert path.is_relative_to(PLUGIN.resolve())
        assert re.fullmatch('[0-9a-f]{40}', entry['blob'])
        assert re.fullmatch('[0-9a-f]{64}', entry['source_sha256'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['packaged_sha256'], path
    for path in PLUGIN.rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        assert path.suffix not in {'.refpt', '.safetensors', '.jsonl', '.log', '.bin'}
        assert not path.is_symlink()
        if path.suffix in {'.md', '.py', '.txt', '.sh', '.json', '.yaml'}:
            assert not re.search(r'/Users/|/home/[^<\s]+|wh-lb-\d+', path.read_text()), path
