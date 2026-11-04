"""Fast-path track: evidence checker, goal templates, gates and calculators, offline."""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-model-bringup'
AUTODEBUG = REPO / 'plugins/tt-autodebug'
TRACK = PLUGIN / 'prompts/model_bringup_fast_path'
CHECKER = PLUGIN / 'scripts/check_fast_path.py'
SCRIPTS = PLUGIN / 'skills/fast-path/scripts'
SHA = 'a' * 40


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)


def common(phase):
    return {'phase': phase, 'hf_model': 'org/model', 'hardware': 'QuietBox 2 (4x p300c)',
            'commits': {'tt-metal': SHA}, 'evidence': ['doc/fast_path/raw.json']}


def acc(top1, top5, pcc=0.99):
    return {'top1': top1, 'top5': top5, 'pcc': pcc}


# Valid evidence per phase, with gpt-oss-20b numbers from the case study.
VALID = {
    'roofline': {
        'peak_gbps_per_chip': 512,
        'meshes': {'1x4': {'tp': 4, 'formats': {'experts': 'bfp8'}, 'gb_per_token': 3.838, 'gb_per_chip': 0.9595,
                           'floor_ms': 1.874, 'baseline_step_ms': 10.4, 'baseline_pct_of_peak': 18.0,
                           'free_dram_gb_per_device': 14.2}},
        'plan': [{'lever': 'streaming decode', 'expected': '10.4 -> about 3.5 ms'}],
    },
    'streaming_decode': {
        'mesh': '1x4', 'device_step_ms': {'before': {'short': 10.4, 'long': 11.0}, 'after': {'short': 3.29, 'long': 3.9}},
        'context_tokens': {'short': 128, 'long': 4096}, 'pct_of_peak_after': 57,
        'accuracy': {'positions': 512, 'stock': acc(96.9, 99.8), 'fast': acc(96.5, 99.7)},
        'served': {'c1_tpot_ms_before': 11.4, 'c1_tpot_ms_after': 3.32},
        'kv_blocks': {'before': 2048, 'after': 2048}, 'decode_only_gb_per_device': 5.6,
        'trace': {'allocation_tracker_clean': True, 'program_cache_growth': 0},
        'fallbacks': ['penalties', 'log-probs'], 'switch': 'MODEL_FUSED_B1=0',
    },
    'mesh_variants': {
        'meshes': {
            '1x4': {'see': 'streaming_decode'},
            '1x1': {'status': 'fused', 'device_step_ms': {'before': {'short': 20.4, 'long': 21.0},
                                                          'after': {'short': 9.29, 'long': 9.9}},
                    'accuracy': {'positions': 512, 'stock': acc(97.0, 99.8), 'fast': acc(96.9, 99.8)},
                    'kv_blocks': {'before': 512, 'after': 512}, 'weights': 'shared_layout', 'profile': 'p150'},
            '1x2': {'status': 'opt_in', 'device_step_ms': {'before': {'short': 13.6, 'long': 14.0},
                                                           'after': {'short': 5.3, 'long': 5.9}},
                    'accuracy': {'positions': 512, 'stock': acc(96.8, 99.8), 'fast': acc(96.6, 99.7)},
                    'kv_blocks': {'before': 1024, 'after': 1024}, 'weights': 'decode_copies', 'profile': 'p150x2',
                    'tradeoff': 'decode copies cost 11.9 GB per device and slow batch 8 by 3%'},
        },
    },
    'multi_user_decode': {
        'mesh': '1x4', 'moe': True,
        'widths': {'1': {'before_ms': 3.29, 'after_ms': 3.29, 'path': 'fused batch-1'},
                   '4': {'before_ms': 12.0, 'after_ms': 5.4, 'path': 'fused multi-row'}},
        'accuracy_by_width': {'1': {'top1': 96.5, 'top5': 99.7}, '4': {'top1': 96.4, 'top5': 99.7}},
        'ladder': [{'concurrency': 4, 'output_tok_s_before': 300.0, 'output_tok_s_after': 520.0}],
        'cost_fit': {'fixed_ms': 2.46, 'per_expert_ms': 0.3595},
    },
    'prefill': {
        'ttft_ms': {'1x4': {'4096': {'before': 506, 'after': 392}, '128': {'before': 40, 'after': 39}}},
        'bit_identical': True, 'digests': {'before': 'f00d', 'after': 'f00d'},
        'deterministic': {'status': 'opt_in', 'runs': 3, 'default_identical': False, 'switch_identical': True,
                          'switch': 'MODEL_DETERMINISTIC_PREFILL=1', 'cost_ms': 12.0},
    },
    'speculative': {
        'decision': 'shipped_opt_in',
        'cost_model': {'normal_ms': 3.32, 'check_ms': 4.51, 'draft_ms': 0.37, 'kept_per_check': 1.72,
                       'estimated_speedup': 1.170},
        'measured': {'speedup': 1.17, 'same_session': True, 'greedy_identical': '12/12', 'multi_user_ratio_min': 0.99},
        'profile': 'p150x4-spec',
        'drafter': {'repo': 'org/model-eagle3', 'revision': 'b' * 40, 'download_on_first_boot': True},
        'active_conditions': ['one running greedy request', 'no log-probs or penalties'],
    },
    'acceptance': {
        'previous_release': 'org/model-package@' + 'c' * 40,
        'ladder': [{'profile': 'p150x4', 'isl': 128, 'concurrency': 1,
                    'before': {'decode_tok_s_u': 88.0, 'output_tok_s': 87.0, 'ttft_ms': 60.0},
                    'after': {'decode_tok_s_u': 306.0, 'output_tok_s': 300.0, 'ttft_ms': 58.0}}],
        'quality_full': {'gsm8k': {'baseline': 86.6, 'after': 86.6}, 'ifeval': {'baseline': 79.7, 'after': 79.8}},
        'quality_c1': {'p150x4': {'gsm8k': 85.2, 'ifeval': 78.0}},
        'smoke': {'chat': True, 'context_limits': True, 'tool_call': True, 'reasoning': True},
        'bandwidth_table': [{'mesh': '1x4', 'width': 1, 'step_ms': 3.29}],
        'conditions_matrix': [{'condition': 'one greedy request', 'path': 'fused batch-1'}],
    },
}


@pytest.fixture
def model(tmp_path):
    root = tmp_path / 'models/autoports/org_model'
    (root / 'doc/fast_path').mkdir(parents=True)
    (root / 'doc/fast_path/raw.json').write_text('{}')
    return root


def check(model, phase, data, cwd):
    (model / 'doc/fast_path' / f'{phase}.json').write_text(json.dumps(data))
    return run(sys.executable, str(CHECKER), '--model-dir', str(model), '--phase', phase, cwd=cwd)


@pytest.mark.parametrize('phase', sorted(VALID))
def test_valid_evidence_passes_and_missing_evidence_is_critical(model, tmp_path, phase):
    result = run(sys.executable, str(CHECKER), '--model-dir', str(model), '--phase', phase, cwd=tmp_path)
    assert result.returncode == 2
    assert 'missing fast-path evidence' in result.stderr
    result = check(model, phase, {**common(phase), **VALID[phase]}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr


def set_path(data, path, value):
    *parents, leaf = path.split('.')
    for part in parents:
        data = data[part] if not isinstance(data, list) else data[int(part)]
    if value is DELETE:
        del data[leaf]
    else:
        data[leaf] = value


DELETE = object()


@pytest.mark.parametrize('phase, path, value, code, message', [
    ('roofline', 'commits', {'tt-metal': 'main'}, 2, 'is not a git SHA'),
    ('roofline', 'evidence', ['doc/fast_path/missing.log'], 2, 'does not exist'),
    ('roofline', 'meshes.1x4.floor_ms', 1.0, 2, 'is not gb_per_chip / peak'),
    ('roofline', 'meshes.1x4.baseline_step_ms', 1.5, 2, 'below the DRAM floor'),
    ('roofline', 'meshes.1x4.baseline_pct_of_peak', 30.0, 1, 'is not floor / step'),
    ('streaming_decode', 'device_step_ms.after.short', 10.5, 2, 'is not faster'),
    ('streaming_decode', 'device_step_ms.after.long', 11.3, 2, 'more than 2% slower'),
    ('streaming_decode', 'accuracy.fast.top1', 96.3, 2, 'top1 96.30 is more than 0.5 points below stock'),
    ('streaming_decode', 'accuracy.fast.top5', 0.994, 2, 'top5 99.40 is more than 0.3 points below stock'),
    ('streaming_decode', 'accuracy.positions', 128, 2, 'at least 256 are required'),
    ('streaming_decode', 'accuracy.positions', 300, 1, '512 are advised'),
    ('streaming_decode', 'served.c1_tpot_ms_after', 4.0, 1, 'host overhead'),
    ('streaming_decode', 'served.c1_tpot_ms_after', 12.0, 2, 'TPOT 12 ms is not faster'),
    ('streaming_decode', 'kv_blocks.after', 1024, 2, 'without hard_limit_evidence'),
    ('streaming_decode', 'trace.allocation_tracker_clean', False, 2, 'unsafe allocations'),
    ('streaming_decode', 'trace.program_cache_growth', 3, 2, 'program cache grew'),
    ('streaming_decode', 'fallbacks', [], 2, 'fallbacks must be a non-empty list'),
    ('streaming_decode', 'switch', DELETE, 2, 'missing switch'),
    ('streaming_decode', 'accuracy', 'fine', 2, 'accuracy must be a non-empty dict'),
    ('mesh_variants', 'meshes.1x1.status', 'stock', 2, 'missing reason'),
    ('mesh_variants', 'meshes.1x1.status', 'faster', 2, 'status must be fused'),
    ('mesh_variants', 'meshes.1x2.tradeoff', DELETE, 2, 'missing tradeoff'),
    ('mesh_variants', 'meshes.1x1.weights', 'bundled', 2, 'weights must be decode_copies or shared_layout'),
    ('mesh_variants', 'meshes.1x1', [1, 2], 2, 'must be an object'),
    ('multi_user_decode', 'widths.1.after_ms', 3.5, 2, 'more than 2% slower'),
    ('multi_user_decode', 'widths.4.after_ms', 12.5, 2, 'no width above 1 got faster'),
    ('multi_user_decode', 'accuracy_by_width.4.top1', 95.9, 2, 'below width 1'),
    ('multi_user_decode', 'ladder.0.output_tok_s_after', 280.0, 2, 'below 97%'),
    ('multi_user_decode', 'cost_fit', DELETE, 2, 'missing cost_fit.fixed_ms'),
    ('multi_user_decode', 'moe', 'yes', 2, 'moe must be a non-empty bool'),
    ('prefill', 'ttft_ms.1x4.128.after', 47.0, 2, 'above 103%'),
    ('prefill', 'ttft_ms.1x4.4096.after', 500.0, 1, 'no prompt length got at least 3% faster'),
    ('prefill', 'digests.after', 'beef', 2, 'digests differ'),
    ('prefill', 'deterministic.runs', 2, 2, 'at least 3 bit-identical runs'),
    ('prefill', 'deterministic.status', 'not_needed', 2, 'not_needed needs at least 3 bit-identical default runs'),
    ('prefill', 'deterministic.status', 'maybe', 2, 'status must be opt_in'),
    ('speculative', 'measured.speedup', 1.05, 2, 'below 1.1'),
    ('speculative', 'measured.greedy_identical', '11/12', 2, 'greedy output must match'),
    ('speculative', 'measured.greedy_identical', '8/8', 2, 'at least 12 prompts'),
    ('speculative', 'measured.same_session', False, 2, 'same session'),
    ('speculative', 'measured.multi_user_ratio_min', 0.95, 2, 'below 0.97'),
    ('speculative', 'drafter.revision', 'main', 2, 'must pin a commit'),
    ('speculative', 'drafter.download_on_first_boot', False, 2, 'not bundled'),
    ('speculative', 'cost_model.estimated_speedup', 1.4, 1, 'is not kept * normal'),
    ('speculative', 'decision', 'maybe', 2, 'decision must be shipped_opt_in or closed'),
    ('acceptance', 'ladder.0.after.output_tok_s', 80.0, 2, 'output_tok_s 80 is below 97%'),
    ('acceptance', 'ladder.0.after.ttft_ms', 70.0, 2, 'TTFT 70 ms is above 103%'),
    ('acceptance', 'quality_full.gsm8k.after', 85.0, 2, 'full gsm8k 85.00 is more than 1.0 points below'),
    ('acceptance', 'quality_full.ifeval.after', 0.78, 2, 'full ifeval 78.00 is more than 1.5 points below'),
    ('acceptance', 'quality_c1.p150x4.gsm8k', 81.0, 2, 'more than 5 points below'),
    ('acceptance', 'quality_c1.p150x4.gsm8k', 83.0, 1, '3-5 points below'),
    ('acceptance', 'smoke.tool_call', False, 2, 'smoke tool_call failed'),
    ('acceptance', 'smoke.context_limits', DELETE, 2, 'missing context_limits'),
    ('acceptance', 'conditions_matrix', [], 2, 'conditions_matrix must be a non-empty list'),
])
def test_checker_thresholds(model, tmp_path, phase, path, value, code, message):
    data = copy.deepcopy({**common(phase), **VALID[phase]})
    set_path(data, path, value)
    result = check(model, phase, data, tmp_path)
    assert result.returncode == code, result.stderr
    assert message in result.stderr
    assert 'Traceback' not in result.stderr


def test_closed_speculation_and_recorded_kv_limit(model, tmp_path):
    data = {**common('speculative'), 'decision': 'closed',
            'cost_model': VALID['speculative']['cost_model'], 'reason': 'short'}
    assert check(model, 'speculative', data, tmp_path).returncode == 2
    data['reason'] = 'check step costs 1.36x a plain step; measured acceptance gives 0.98x'
    assert check(model, 'speculative', data, tmp_path).returncode == 0
    data = copy.deepcopy({**common('streaming_decode'), **VALID['streaming_decode']})
    data['kv_blocks']['after'] = 1900
    data['hard_limit_evidence'] = 'doc/fast_path/raw.json: 31.4 of 32 GB in use at 1900 blocks'
    result = check(model, 'streaming_decode', data, tmp_path)
    assert result.returncode == 1
    assert 'under a recorded hard limit' in result.stderr


def test_wrong_phase_malformed_file_and_bad_arguments(model, tmp_path):
    result = check(model, 'roofline', {**common('prefill'), **VALID['roofline']}, tmp_path)
    assert result.returncode == 2
    assert "expected 'roofline'" in result.stderr
    (model / 'doc/fast_path/prefill.json').write_text('[1, 2]')
    result = run(sys.executable, str(CHECKER), '--model-dir', str(model), '--phase', 'prefill', cwd=tmp_path)
    assert result.returncode == 2
    assert 'top level must be a JSON object' in result.stderr
    (model / 'doc/fast_path/prefill.json').write_text('{')
    result = run(sys.executable, str(CHECKER), '--model-dir', str(model), '--phase', 'prefill', cwd=tmp_path)
    assert result.returncode == 3
    result = run(sys.executable, str(CHECKER), '--phase', 'prefill', cwd=tmp_path)
    assert result.returncode == 3
    result = run(sys.executable, str(CHECKER), '--model-dir', str(model), '--phase', 'nope', cwd=tmp_path)
    assert result.returncode == 3


def test_hf_model_scope_resolves_the_autoport_directory(model, tmp_path):
    (model / 'doc/fast_path/roofline.json').write_text(json.dumps({**common('roofline'), **VALID['roofline']}))
    result = run(sys.executable, str(CHECKER), '--hf-model', 'Org/Model', '--phase', 'roofline', cwd=tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def installed(tmp_path):
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


def test_track_runs_as_its_own_dry_run(installed):
    plugin, target, env = installed
    prompts = sorted((plugin / 'prompts/model_bringup_fast_path').glob('*.txt'))
    assert [p.stem for p in prompts] == ['01-roofline', '02-streaming-decode', '03-mesh-variants',
                                         '04-multi-user-decode', '05-prefill', '06-speculative-decoding',
                                         '07-fast-path-acceptance']
    result = run(sys.executable, str(plugin / 'scripts/multigoal'), *map(str, prompts),
                 '--replace', 'HF_MODEL=org/model', '--replace', 'MODEL_DIR=models/autoports/org_model',
                 '--dry-run', cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert 'missing skill' not in result.stderr
    manifests = list(target.glob('bringup/artifacts/multigoal-runs/*/manifest.txt'))
    assert len(manifests) == 1
    manifest = load('fast_path_runner', PLUGIN / 'scripts/multigoal').read_manifest(manifests[0])
    assert manifest['stage_7_dry_run'] == 'true'
    assert manifest['stage_7_check_script'].endswith('/07-fast-path-acceptance.check.sh')
    assert manifest['stage_1_check_script'].startswith(str(plugin))
    assert len(list(manifests[0].parent.glob('*.prompt.txt'))) == 7


@pytest.mark.parametrize('stem', [p.stem for p in sorted(TRACK.glob('*.txt'))])
def test_goal_objectives_fit_and_name_installed_skills(stem, monkeypatch):
    monkeypatch.setenv('TT_AUTODEBUG_ROOT', str(AUTODEBUG))
    runner = load('fast_path_goal_runner', PLUGIN / 'scripts/multigoal')
    objective = runner.objective_from_prompt(runner.load_prompt(TRACK / f'{stem}.txt', [('HF_MODEL', 'org/model')]))
    items, missing = runner.input_items_for_objective(Path('/unused'), objective)
    assert not missing
    assert '$fast-path' in objective and '$stage-review' in objective
    assert (TRACK / f'{stem}.check.sh').is_file()


@pytest.mark.parametrize('name', [p.name for p in sorted(TRACK.glob('*.check.sh'))])
def test_installed_gates_fail_critical_without_evidence_and_need_a_scope(installed, name):
    plugin, target, env = installed
    gate = plugin / 'prompts/model_bringup_fast_path' / name
    env.pop('MODEL_DIR', None)
    env.pop('HF_MODEL', None)
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 3
    env['MODEL_DIR'] = 'models/autoports/org_model'
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 2, result.stderr
    assert "can't open file" not in result.stderr
    assert 'missing fast-path evidence' in result.stderr


def test_installed_gate_passes_with_evidence_and_context_contract(installed):
    plugin, target, env = installed
    model = target / 'models/autoports/org_model'
    (model / 'doc/fast_path').mkdir(parents=True)
    (model / 'doc/fast_path/raw.json').write_text('{}')
    (model / 'doc/fast_path/streaming_decode.json').write_text(
        json.dumps({**common('streaming_decode'), **VALID['streaming_decode']}))
    env['MODEL_DIR'] = 'models/autoports/org_model'
    gate = plugin / 'prompts/model_bringup_fast_path/02-streaming-decode.check.sh'
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 2
    assert 'Missing context contract' in result.stderr
    (model / 'doc/context_contract.json').write_text(
        json.dumps({'hf_advertised_context': 131072, 'current_supported_context': 131072}))
    result = run('bash', str(gate), cwd=target, env=env)
    assert result.returncode == 0, result.stderr + result.stdout


def test_roofline_calculator_reproduces_the_reference_model(tmp_path):
    roofline = load('fast_path_roofline', SCRIPTS / 'fast_path_roofline.py')
    config = {'hidden_size': 2880, 'num_hidden_layers': 24, 'num_attention_heads': 64, 'num_key_value_heads': 8,
              'head_dim': 64, 'vocab_size': 201088, 'num_local_experts': 32, 'num_experts_per_tok': 4,
              'intermediate_size': 2880, 'sliding_window': 128,
              'layer_types': ['sliding_attention', 'full_attention'] * 12}
    result = roofline.count(config, dict(roofline.DEFAULT_FORMATS), {}, 128, 1, 3)
    assert result['model']['moe_layers'] == 24
    assert result['gb_per_token'] == pytest.approx(3.838, abs=0.01)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    out = tmp_path / 'roofline.json'
    proc = run(sys.executable, str(SCRIPTS / 'fast_path_roofline.py'), '--config', str(path), '--tp', '4',
               '--measured-ms', '4=3.39', '--json', str(out), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    mesh = json.loads(out.read_text())['meshes']['4']
    assert mesh['floor_ms'] == pytest.approx(1.87, abs=0.01)
    assert mesh['pct_of_peak'] == pytest.approx(55, abs=1)
    dense = roofline.count({'hidden_size': 4096, 'num_hidden_layers': 32, 'num_attention_heads': 32,
                            'num_key_value_heads': 8, 'vocab_size': 128256, 'intermediate_size': 14336},
                           dict(roofline.DEFAULT_FORMATS), {}, 128, 1, 3)
    assert dense['model']['dense_layers'] == 32 and 'experts' not in dense['gb_per_token_by_role']
    with pytest.raises(SystemExit, match='latent attention'):
        roofline.count({**config, 'kv_lora_rank': 512}, dict(roofline.DEFAULT_FORMATS), {}, 128, 1, 3)


def test_expert_union_counts_consecutive_and_independent_rows(tmp_path):
    union = load('fast_path_union', SCRIPTS / 'expert_union.py')
    seq = [[[0, 1]], [[1, 2]], [[2, 3]], [[0, 1]]]
    assert union.union_per_layer(seq[:2]) == 3
    assert union.consecutive([seq], 2) == [3, 3, 4]
    assert union.summarize(union.consecutive([seq], 4))['mean'] == 4
    assert len(union.independent([seq], 2, samples=10, seed=0)) == 10
    path = tmp_path / 'ids.json'
    path.write_text(json.dumps(seq))
    proc = run(sys.executable, str(SCRIPTS / 'expert_union.py'), '--ids', str(path), '--experts', '4',
               '--widths', '1', '2', '--json', str(tmp_path / 'u.json'), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((tmp_path / 'u.json').read_text())['widths']['1']['consecutive']['mean'] == 2


def test_speculation_cost_model_reproduces_the_measured_speedup(tmp_path):
    spec = load('fast_path_spec', SCRIPTS / 'spec_cost_model.py')
    result = spec.model(3.32, 4.51, 0.37, 1.72, 1, 1.3)
    assert result['speedup'] == pytest.approx(1.170, abs=0.002)
    assert result['ceiling_perfect_drafts'] == pytest.approx(1.361, abs=0.002)
    assert spec.expected_kept([0.5, 0.5]) == pytest.approx(1.75)
    proc = run(sys.executable, str(SCRIPTS / 'spec_cost_model.py'), '--normal-ms', '3.32', '--check-fixed-ms', '2.46',
               '--check-per-expert-ms', '0.3595', '--check-union', '6.4', '--draft-ms', '0.37', '--kept', '1.72',
               cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert 'speedup' in proc.stdout
    proc = run(sys.executable, str(SCRIPTS / 'spec_cost_model.py'), '--normal-ms', '3', '--check-ms', '4',
               '--draft-ms', '0.4', '--kept', '3', cwd=tmp_path)
    assert proc.returncode == 2
    assert 'must lie in' in proc.stderr
