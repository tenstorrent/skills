"""Per-stage token and time usage: counted from CODEX_HOME rollouts, written by the runner, gated."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-model-bringup'
GATES = PLUGIN / 'prompts/model_bringup_multigoal'
sys.path.insert(0, str(PLUGIN / 'scripts'))
import stage_usage  # noqa: E402

loader = importlib.machinery.SourceFileLoader('stage_usage_runner', str(PLUGIN / 'scripts/multigoal'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = runner
loader.exec_module(runner)

START = '2026-11-12T10:00:00Z'
END = '2026-11-12T12:00:00Z'


def usage(i, c, o, r):
    return {'input_tokens': i, 'cached_input_tokens': c, 'cache_write_input_tokens': 0,
            'output_tokens': o, 'reasoning_output_tokens': r, 'total_tokens': i + o}


def token_count(ts, last, total):
    info = None if last is None else {'last_token_usage': usage(*last), 'total_token_usage': usage(*total),
                                      'model_context_window': 258400}
    return {'timestamp': ts, 'type': 'event_msg',
            'payload': {'type': 'token_count', 'info': info,
                        'rate_limits': {'primary': {'used_percent': 33.0}}}}


def session(home, day, name, sid, ts, events, *, source='vscode', originator='tt-metal-multigoal',
            model='gpt-test', parent=None, depth=None):
    if parent:
        source = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': depth,
                                                'agent_path': '/root/x'}}}
    lines = [{'timestamp': ts, 'type': 'session_meta',
              'payload': {'id': sid, 'timestamp': ts, 'originator': originator, 'source': source,
                          'parent_thread_id': parent}},
             {'timestamp': ts, 'type': 'turn_context', 'payload': {'model': model}}]
    folder = home / 'sessions' / day
    folder.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps(line) for line in lines + events]
    # Same usage as a token_usage_record: never counted a second time.
    rows.append(json.dumps({'timestamp': ts, 'type': 'token_usage_record', 'payload': {'usage': usage(9, 9, 9, 9)}}))
    rows.append('not json')
    (folder / f'rollout-{name}-{sid}.jsonl').write_text('\n'.join(rows) + '\n')


@pytest.fixture
def home(tmp_path):
    home = tmp_path / 'codex-home'
    # Main thread: one event before the window, a re-emitted (unchanged total) event, one after.
    session(home, '2026/11/12', '2026-11-12T09-59-00', 'main-1', '2026-11-12T09:59:00Z', [
        token_count('2026-11-12T09:59:30Z', (100, 0, 10, 1), (100, 0, 10, 1)),
        token_count('2026-11-12T10:10:00Z', (200, 150, 20, 5), (300, 150, 30, 6)),
        token_count('2026-11-12T10:10:01Z', (200, 150, 20, 5), (300, 150, 30, 6)),
        token_count('2026-11-12T10:15:00Z', None, None),
        token_count('2026-11-12T11:00:00Z', (1000, 800, 50, 10), (1300, 950, 80, 16)),
        token_count('2026-11-12T12:30:00Z', (5000, 0, 0, 0), (6300, 950, 80, 16)),
    ])
    # Sub-agent and a sub-agent of that sub-agent (stored under another date: names do not matter).
    session(home, '2026/11/12', '2026-11-12T10-19-00', 'sub-1', '2026-11-12T10:19:00Z', [
        token_count('2026-11-12T10:20:00Z', (400, 100, 40, 4), (400, 100, 40, 4)),
        token_count('2026-11-12T10:30:00Z', (600, 500, 60, 6), (1000, 600, 100, 10)),
    ], parent='main-1', depth=1, model='gpt-sub')
    session(home, '2026/11/01', '2026-11-01T00-00-00', 'sub-2', '2026-11-12T10:39:00Z', [
        token_count('2026-11-12T10:40:00Z', (50, 25, 5, 0), (50, 25, 5, 0)),
    ], parent='sub-1', depth=2, model='gpt-sub')
    # Another thread in the home that started long before the window.
    session(home, '2026/11/12', '2026-11-12T08-00-00', 'exec-1', '2026-11-12T08:00:00Z', [
        token_count('2026-11-12T08:30:00Z', (9999, 0, 99, 9), (9999, 0, 99, 9)),
        token_count('2026-11-12T11:30:00Z', (70, 30, 7, 2), (10069, 30, 106, 11)),
    ], source='exec', originator='codex_exec')
    # Entirely after the window.
    session(home, '2026/11/12', '2026-11-12T12-10-00', 'late-1', '2026-11-12T12:10:00Z', [
        token_count('2026-11-12T12:15:00Z', (1, 1, 1, 1), (1, 1, 1, 1)),
    ])
    return home


def row(sessions, i, c, o, r, models):
    return {'sessions': sessions, 'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o,
            'reasoning_output_tokens': r, 'uncached_input_tokens': i - c, 'models': models}


EXPECTED = {
    'main_thread': row(1, 1200, 950, 70, 15, ['gpt-test']),
    'sub_agents': row(2, 1050, 625, 105, 10, ['gpt-sub']),
    'other_threads': row(1, 70, 30, 7, 2, ['gpt-test']),
    'total': row(4, 2320, 1605, 182, 27, ['gpt-sub', 'gpt-test']),
}


def test_window_sums_are_exact(home):
    rows = stage_usage.compute_usage(home, stage_usage.parse_ts(START), stage_usage.parse_ts(END), ['main-1'])
    assert rows == EXPECTED


def test_main_thread_defaults_to_runner_threads_and_ids_are_respected(home):
    start, end = stage_usage.parse_ts(START), stage_usage.parse_ts(END)
    assert stage_usage.compute_usage(home, start, end) == EXPECTED
    rows = stage_usage.compute_usage(home, start, end, ['someone-else'])
    assert rows['main_thread']['sessions'] == 0
    assert rows['other_threads']['input_tokens'] == 1270
    assert rows['total'] == EXPECTED['total']


def model_tree(tmp_path, stage_report='doc/agentic_evals/REPORT.md'):
    repo = tmp_path / 'checkout'
    model = repo / 'models/autoports/org_model'
    report = model / stage_report
    report.parent.mkdir(parents=True)
    report.write_text('# Agentic evals\n\nPhase 1 passed.\n\n## Stage usage\n\nold numbers\n\n## Analysis\n\nkept\n')
    for args in (['init', '-q'], ['config', 'user.email', 't@example.com'], ['config', 'user.name', 't']):
        subprocess.run(['git', *args], cwd=repo, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True)
    return repo, model, report


def test_runner_writes_section_and_json_commits_them_and_rewrites_after_remediation(tmp_path, home, monkeypatch):
    repo, model, report = model_tree(tmp_path)
    (repo / 'unrelated.txt').write_text('agent work in progress')
    subprocess.run(['git', 'add', 'unrelated.txt'], cwd=repo, check=True)
    log = tmp_path / 'log'
    log.mkdir()
    manifest = log / 'manifest.txt'
    manifest.write_text('')
    clock = iter(['2026-11-12T10:00:00Z', '2026-11-12T11:00:00Z', '2026-11-12T11:00:00Z',
                  '2026-11-12T11:30:00Z', END])
    monkeypatch.setattr(runner, 'iso_now', lambda: next(clock))
    args = SimpleNamespace(_codex_home=home)
    usage = runner.StageUsage(args, repo, manifest, 12, '12-agentic-evals', Path('12-agentic-evals.txt'), '',
                              [('MODEL_DIR', 'models/autoports/org_model')])
    usage.thread('main-1')
    usage.write('complete')
    first = json.loads((model / 'doc/agentic_evals/stage_usage.json').read_text())
    assert first['end_utc'] == '2026-11-12T11:00:00Z'
    assert first['rows']['total']['input_tokens'] == 200 + 1000 + 400 + 600 + 50  # end is inclusive
    assert first['remediation_rounds'] == []

    usage.begin_round('remediation')
    usage.end_round('complete')
    usage.write('complete')
    record = json.loads((model / 'doc/agentic_evals/stage_usage.json').read_text())
    assert record['rows'] == EXPECTED
    assert record['wall_hours'] == 2.0
    assert record['remediation_rounds'] == [{'kind': 'remediation', 'round': 1, 'start_utc': '2026-11-12T11:00:00Z',
                                             'end_utc': '2026-11-12T11:30:00Z', 'terminal_status': 'complete'}]
    text = report.read_text()
    assert text.count('## Stage usage') == 1 and 'old numbers' not in text and '## Analysis\n\nkept' in text
    assert '| total | 4 | 2,320 | 1,605 | 715 | 182 | 27 | gpt-sub, gpt-test |' in text
    assert '| sub-agents (2) | 2 | 1,050 |' in text
    assert 'remediation rounds 1 (remediation 1 2026-11-12T11:00:00Z to 2026-11-12T11:30:00Z, complete)' in text
    assert f'Counted from the Codex sessions in {home} during the stage window; other homes on the same account are not included.' in text
    assert stage_usage.check(model, '12-agentic-evals') == (0, [])

    log_lines = subprocess.run(['git', 'log', '--format=%s'], cwd=repo, capture_output=True, text=True).stdout.split('\n')
    assert log_lines[:2] == ['12-agentic-evals: stage usage (complete)'] * 2
    committed = subprocess.run(['git', 'show', '--name-only', '--format=', 'HEAD'], cwd=repo,
                               capture_output=True, text=True).stdout.split()
    assert sorted(committed) == ['models/autoports/org_model/doc/agentic_evals/REPORT.md',
                                 'models/autoports/org_model/doc/agentic_evals/stage_usage.json']
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=repo, capture_output=True, text=True).stdout
    assert staged.split() == ['unrelated.txt']
    assert 'stage_12_started_at_utc=2026-11-12T10:00:00Z' in manifest.read_text()


def written(tmp_path, home, stage='12-agentic-evals'):
    repo, model, report = model_tree(tmp_path, stage_usage.STAGE_REPORTS[stage_usage.stage_key(stage)][0])
    paths = stage_usage.stage_paths(stage)
    record = stage_usage.usage_record(stage=stage, home=home, start=stage_usage.parse_ts(START),
                                      end=stage_usage.parse_ts(END), terminal_status='complete', rounds=[],
                                      report=paths[0])
    stage_usage.write_usage(model, *paths, record)
    return model, report, model / paths[1] / 'stage_usage.json'


def gate(model, stage, required=True):
    env = {k: v for k, v in os.environ.items() if k != 'MULTIGOAL_STAGE_USAGE'}
    if required:
        env['MULTIGOAL_STAGE_USAGE'] = '1'
    return subprocess.run([sys.executable, str(PLUGIN / 'scripts/stage_usage.py'), 'check', '--model-dir', str(model),
                           '--stage', stage], env=env, capture_output=True, text=True)


@pytest.mark.parametrize('break_it, message', [
    (lambda report, js: js.unlink(), 'stage_usage.json is missing'),
    (lambda report, js: js.write_text('{'), 'not valid JSON'),
    (lambda report, js: js.write_text(json.dumps({**json.loads(js.read_text()), 'rows': {}})), 'rows must have'),
    (lambda report, js: js.write_text(json.dumps(
        {**json.loads(js.read_text()), 'end_utc': '2026-11-12T09:00:00Z'})), 'end >= start'),
    (lambda report, js: report.write_text('# Report\n'), "exactly one '## Stage usage'"),
    (lambda report, js: report.write_text(report.read_text().replace('| total | 4 | 2,320', '| total | 4 | 2,321')),
     'total row does not match'),
    (lambda report, js: report.write_text(report.read_text().replace('Counted from', 'Taken from')),
     'Counted from the Codex sessions'),
    (lambda report, js: report.unlink(), 'REPORT.md is missing'),
])
def test_missing_or_malformed_usage_is_critical(tmp_path, home, break_it, message):
    model, report, js = written(tmp_path, home)
    assert gate(model, '12-agentic-evals').returncode == 0
    break_it(report, js)
    result = gate(model, '12-agentic-evals')
    assert result.returncode == 2, result.stderr
    assert message in result.stderr
    # An agent running the check itself mid-stage is not judged on the runner's record.
    assert gate(model, '12-agentic-evals', required=False).returncode == 0


def test_no_thresholds_on_the_numbers(tmp_path):
    empty = tmp_path / 'empty-home'
    (empty / 'sessions').mkdir(parents=True)
    model, _report, js = written(tmp_path, empty, '11-benchmark')
    assert js == model / 'doc/benchmark/stage_usage.json'
    assert json.loads(js.read_text())['rows']['total']['input_tokens'] == 0
    assert gate(model, '11-benchmark').returncode == 0


def test_missing_report_is_not_created(tmp_path, home):
    model = tmp_path / 'model'
    record = stage_usage.usage_record(stage='09-vllm', home=home, start=stage_usage.parse_ts(START),
                                      end=stage_usage.parse_ts(END), terminal_status='blocked', rounds=[],
                                      report='doc/vllm_integration/README.md')
    paths = stage_usage.write_usage(model, 'doc/vllm_integration/README.md', 'doc/vllm_integration', record)
    assert paths == [model / 'doc/vllm_integration/stage_usage.json']
    assert not (model / 'doc/vllm_integration/README.md').exists()
    assert gate(model, '09-vllm').returncode == 2


def test_every_stage_has_a_report_and_every_check_gates_usage():
    for prompt in sorted(GATES.glob('[0-9][0-9]-*.txt')):
        assert stage_usage.stage_paths(prompt.stem), prompt
        report = stage_usage.stage_paths(prompt.stem)[0]
        text = prompt.read_text()
        assert report in text or report.split('/')[1].replace('_', '') in text.replace('_', '').replace('-', ''), prompt
    checks = sorted(GATES.glob('*.check.sh'))
    assert [c.name[:2] for c in checks] == ['06', '07', '09', '10', '11', '12']
    for check in checks:
        stem = check.name[:-len('.check.sh')]
        assert f'scripts/stage_usage.py" check --model-dir' in check.read_text(), check
        assert f'--stage {stem} ' in check.read_text(), check


def test_runner_gates_usage_only_on_its_own_check_runs():
    source = (PLUGIN / 'scripts/multigoal').read_text()
    assert 'extra_env["MULTIGOAL_STAGE_USAGE"] = "1"' in source
    assert 'usage_budget' not in source and 'rateLimits' not in source and 'used_percent' not in source


def test_usage_budget_is_gone():
    for path in PLUGIN.rglob('*'):
        if path.is_file() and path.suffix in {'.md', '.txt', '.sh', '.py', '.json'} and '__pycache__' not in path.parts:
            assert 'usage_budget' not in path.read_text(errors='replace'), path
