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
import device_timeline  # noqa: E402
from test_device_timeline import FakeHost  # noqa: E402

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
    # Main thread: every event counts (also those outside any window); a re-emitted event
    # (unchanged total) and a null-info event do not.
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
    # A remediation thread the runner started, with its own sub-agent.
    session(home, '2026/11/12', '2026-11-12T11-40-00', 'rem-1', '2026-11-12T11:40:00Z', [
        token_count('2026-11-12T11:41:00Z', (300, 100, 30, 3), (300, 100, 30, 3)),
        token_count('2026-11-12T13:41:00Z', (200, 0, 20, 2), (500, 100, 50, 5)),
    ])
    session(home, '2026/11/12', '2026-11-12T11-45-00', 'rem-sub', '2026-11-12T11:45:00Z', [
        token_count('2026-11-12T11:46:00Z', (80, 40, 8, 1), (80, 40, 8, 1)),
    ], parent='rem-1', depth=1, model='gpt-sub')
    # Other sessions in the home: not counted, only listed by number (a codex exec thread and
    # its sub-agent, and a later runner thread that is not this stage's).
    session(home, '2026/11/12', '2026-11-12T08-00-00', 'exec-1', '2026-11-12T08:00:00Z', [
        token_count('2026-11-12T11:30:00Z', (70, 30, 7, 2), (70, 30, 7, 2)),
    ], source='exec', originator='codex_exec')
    session(home, '2026/11/12', '2026-11-12T08-10-00', 'exec-sub', '2026-11-12T08:10:00Z', [
        token_count('2026-11-12T11:31:00Z', (9, 9, 9, 9), (9, 9, 9, 9)),
    ], parent='exec-1', depth=1, originator='codex_exec')
    session(home, '2026/11/12', '2026-11-12T12-10-00', 'late-1', '2026-11-12T12:10:00Z', [
        token_count('2026-11-12T12:15:00Z', (1, 1, 1, 1), (1, 1, 1, 1)),
    ])
    return home


def row(sessions, i, c, o, r, models):
    return {'sessions': sessions, 'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o,
            'reasoning_output_tokens': r, 'uncached_input_tokens': i - c, 'models': models}


EXPECTED = {
    'main_thread': row(1, 6300, 950, 80, 16, ['gpt-test']),
    'sub_agents': row(2, 1050, 625, 105, 10, ['gpt-sub']),
    'rounds': row(2, 580, 140, 58, 6, ['gpt-sub', 'gpt-test']),
    'total': row(5, 7930, 1715, 243, 32, ['gpt-sub', 'gpt-test']),
}


def test_thread_family_sums_are_exact(home):
    rows, other = stage_usage.compute_usage(home, ['main-1'], ['rem-1'])
    assert rows == EXPECTED
    assert other == 3


def test_only_the_runners_thread_ids_define_the_family(home):
    rows, other = stage_usage.compute_usage(home, ['main-1'])
    assert rows['rounds']['sessions'] == 0 and rows['total']['input_tokens'] == 7350 and other == 5
    rows, other = stage_usage.compute_usage(home, ['someone-else'], ['rem-1'])
    assert rows['main_thread']['sessions'] == 0 and rows['sub_agents']['sessions'] == 0
    assert rows['rounds']['sessions'] == 2 and other == 6
    # A parent loop does not hang the walk.
    session(home, '2026/11/12', 'loop-a', 'loop-a', '2026-11-12T10:00:00Z', [], parent='loop-b', depth=1)
    session(home, '2026/11/12', 'loop-b', 'loop-b', '2026-11-12T10:00:00Z', [], parent='loop-a', depth=1)
    assert stage_usage.compute_usage(home, ['main-1'], ['rem-1'])[1] == 5


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
    host = FakeHost(tmp_path)
    host.process(200, 100, 'python3 server.py', devices=[0])
    args = SimpleNamespace(_codex_home=home, _proc_root=host.proc, _dev_dir=host.dev)
    usage = runner.StageUsage(args, repo, manifest, 12, '12-agentic-evals', Path('12-agentic-evals.txt'), '',
                              [('MODEL_DIR', 'models/autoports/org_model')])
    usage.thread('main-1')
    usage.start_timeline(fresh=True)
    usage.write('complete')
    first = json.loads((model / 'doc/agentic_evals/stage_usage.json').read_text())
    assert first['end_utc'] == '2026-11-12T11:00:00Z'
    assert first['rows']['total']['input_tokens'] == 6300 + 1050  # no rounds yet: not counted
    assert first['remediation_rounds'] == []

    usage.thread('rem-1', 'round')
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
    assert '| total | 5 | 7,930 | 1,715 | 6,215 | 243 | 32 | gpt-sub, gpt-test |' in text
    assert '| sub-agents (2) | 2 | 1,050 |' in text
    assert 'remediation rounds 1 (remediation 1 2026-11-12T11:00:00Z to 2026-11-12T11:30:00Z, complete)' in text
    assert f'from the Codex sessions in {home}; 3 other session(s) in that home are not counted' in text
    assert '| remediation/review rounds | 2 | 580 |' in text and '| main | 1 | 6,300 |' in text
    usage.close()
    device = record['device']
    assert device['timeline'] == 'device_timeline.jsonl' and device['wall_s'] <= 2
    assert sum(device[key] for key in stage_usage.DEVICE_KEYS) == device['wall_s']
    assert 'wall hours; held by other holders' in text and 'an idle server counts as held' in text
    assert stage_usage.check(model, '12-agentic-evals') == (0, [])

    log_lines = subprocess.run(['git', 'log', '--format=%s'], cwd=repo, capture_output=True, text=True).stdout.split('\n')
    assert log_lines[:2] == ['12-agentic-evals: stage usage (complete)'] * 2
    committed = subprocess.run(['git', 'show', '--name-only', '--format=', 'HEAD'], cwd=repo,
                               capture_output=True, text=True).stdout.split()
    assert sorted(committed) == ['models/autoports/org_model/doc/agentic_evals/REPORT.md',
                                 'models/autoports/org_model/doc/agentic_evals/device_timeline.jsonl',
                                 'models/autoports/org_model/doc/agentic_evals/stage_usage.json']
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=repo, capture_output=True, text=True).stdout
    assert staged.split() == ['unrelated.txt']
    assert 'stage_12_started_at_utc=2026-11-12T10:00:00Z' in manifest.read_text()


def written(tmp_path, home, stage='12-agentic-evals'):
    repo, model, report = model_tree(tmp_path, stage_usage.STAGE_REPORTS[stage_usage.stage_key(stage)][0])
    paths = stage_usage.stage_paths(stage)
    timeline = model / paths[1] / 'device_timeline.jsonl'
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_text('\n'.join(json.dumps(item) for item in [
        {'t': 0, 'held': 0, 'of': 2, 'by': '', 'other': False, 'event': 'stage_start', 'host': 'h1', 'uid': 7,
         'pidns': 'pid:[1]', 'interval': 15},
        {'t': 3600, 'held': 1, 'of': 2, 'by': 'server', 'other': False},
        {'t': 7200, 'held': 1, 'of': 2, 'by': 'x', 'other': True},
        {'t': 7500, 'held': 0, 'of': 2, 'by': '', 'other': False, 'event': 'stage_end'}]) + '\n')
    record = stage_usage.usage_record(stage=stage, device=device_timeline.summarize(timeline), home=home, start=stage_usage.parse_ts(START),
                                      end=stage_usage.parse_ts(END), terminal_status='complete', rounds=[],
                                      report=paths[0], main_thread_ids=['main-1'],
                                      round_thread_ids=['rem-1'])
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
    (lambda report, js: report.write_text(report.read_text().replace('| total | 5 | 7,930', '| total | 5 | 7,931')),
     'total row does not match'),
    (lambda report, js: report.write_text(report.read_text().replace('Counted by', 'Taken by')),
     'Counted by thread family'),
    (lambda report, js: report.unlink(), 'REPORT.md is missing'),
    (lambda report, js: (js.parent / 'device_timeline.jsonl').unlink(), 'device_timeline.jsonl is missing'),
    (lambda report, js: js.write_text(json.dumps({k: v for k, v in json.loads(js.read_text()).items()
                                                  if k != 'device'})), 'device (held/idle/unknown hours) is missing'),
    (lambda report, js: report.write_text(''.join(line for line in report.read_text().splitlines(True)
                                                  if not line.startswith('Device '))), 'device held/idle/unknown row'),
    (lambda report, js: report.write_text(report.read_text().replace('held 1.00 h', 'held 1.01 h')),
     'device held/idle/unknown row'),
    (lambda report, js: js.write_text(json.dumps({**json.loads(js.read_text()), 'device': {
        **json.loads(js.read_text())['device'], 'idle_s': 1}})), 'device held + other + idle + unknown != wall'),
])
def test_missing_or_malformed_usage_is_critical(tmp_path, home, break_it, message):
    """Critical, except the device row: the runner's own measurement the stage cannot fix is advisory."""
    model, report, js = written(tmp_path, home)
    assert gate(model, '12-agentic-evals').returncode == 0
    break_it(report, js)
    result = gate(model, '12-agentic-evals')
    assert result.returncode == (1 if 'device' in message else 2), result.stderr
    assert message in result.stderr
    # An agent running the check itself mid-stage is not judged on the runner's record.
    assert gate(model, '12-agentic-evals', required=False).returncode == 0


def test_device_row_is_exact_and_notes_held_not_busy(tmp_path, home):
    _model, report, js = written(tmp_path, home)
    device = json.loads(js.read_text())['device']
    assert (device['held_s'], device['held_other_s'], device['idle_s'], device['unknown_s'], device['wall_s']) == (
        3600, 300, 3600, 0, 7500)
    assert device['complete'] is True and device['host'] == 'h1' and device['uid'] == 7
    assert ('held 1.00 h, idle 1.00 h, unknown 0.00 h, of 2.08 wall hours; held by other holders 0.08 h '
            '(separate). Held, not busy: an idle server counts as held.') in report.read_text()
    assert 'ran on h1, as uid 7, pid namespace pid:[1]; view complete' in report.read_text()


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
    assert 'extra_env["MULTIGOAL_STAGE_USAGE"] = f"unwritten: {usage.unwritten}" if usage.unwritten else "1"' in source
    assert 'usage_budget' not in source and 'rateLimits' not in source and 'used_percent' not in source


def test_usage_budget_is_gone():
    for path in PLUGIN.rglob('*'):
        if path.is_file() and path.suffix in {'.md', '.txt', '.sh', '.py', '.json'} and '__pycache__' not in path.parts:
            assert 'usage_budget' not in path.read_text(errors='replace'), path


def test_a_record_the_runner_could_not_write_is_reported_not_critical(tmp_path):
    """The stage's agent cannot fix the runner's own write: advisory, so the pipeline does not stop on it."""
    env = {**os.environ, 'MULTIGOAL_STAGE_USAGE': 'unwritten: OSError: disk full'}
    result = subprocess.run([sys.executable, str(PLUGIN / 'scripts/stage_usage.py'), 'check', '--model-dir',
                             str(tmp_path), '--stage', '12-agentic-evals'], env=env, capture_output=True, text=True)
    assert result.returncode == 1 and 'disk full' in result.stderr


def test_a_clock_step_back_keeps_the_partition_exact(tmp_path):
    """Wall is the sum of the intervals: a backward clock step or out-of-order lines cannot break held+idle+unknown."""
    timeline = tmp_path / 'device_timeline.jsonl'
    timeline.write_text('\n'.join(json.dumps(item) for item in [
        {'t': 1000, 'held': 1, 'of': 1, 'by': 's', 'other': False, 'event': 'stage_start', 'interval': 15},
        {'t': 1600, 'held': 0, 'of': 1, 'by': '', 'other': False},
        {'t': 1300, 'held': 1, 'of': 1, 'by': 's', 'other': False},              # clock stepped back
        {'t': 2000, 'held': 0, 'of': 1, 'by': '', 'other': False, 'event': 'stage_end'}]) + '\n')
    summary = device_timeline.summarize(timeline)
    parts = summary['held_s'] + summary['held_other_s'] + summary['idle_s'] + summary['unknown_s']
    assert parts == summary['wall_s'] and summary['complete'] is False and summary['span_s'] == 1000
    assert stage_usage.validate_device(summary) == []
