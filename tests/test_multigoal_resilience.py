"""Runner resilience: transient retries, usage waits, notify, steering, remediation modes."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins/tt-model-bringup'
loader = importlib.machinery.SourceFileLoader('bringup_runner_resilience', str(PLUGIN / 'scripts/multigoal'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = runner
loader.exec_module(runner)

OVERLOADED = {'message': 'Selected model is at capacity. Please try a different model.',
              'codexErrorInfo': 'serverOverloaded', 'additionalDetails': None}
STREAM_503 = {'message': 'Reconnecting... 5/5', 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 503}}}
USAGE = {'message': 'You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more '
                    'credits or try again at Oct 22nd, 2026 6:14 AM.', 'codexErrorInfo': 'usageLimitExceeded'}


class FakeAppServer:
    """In-process stand-in for codex app-server.

    Each turn/start consumes one scripted outcome and queues the notifications
    a real server would emit for it.
    """

    def __init__(self, outcomes, rate_limits=None):
        self.outcomes = list(outcomes)
        self.rate_limits = list(rate_limits or [])
        self.pending = []
        self.calls = []
        self.goals = {}
        self.threads = 0
        self.loaded_threads = set()
        self.latest_rate_limits = None

    def request(self, method, params, log, on_event=None, timeout=None):
        self.calls.append((method, params))
        if method == 'thread/start':
            self.threads += 1
            return {'thread': {'id': f'thread-{self.threads}'}}
        if method == 'thread/resume':
            return {'thread': {'id': params['threadId'], 'turns': []}}
        if method == 'thread/goal/set':
            goal = self.goals.setdefault(params['threadId'], {})
            if 'objective' in params:
                goal['objective'] = params['objective']
            goal['status'] = params['status']
            return {'goal': dict(goal)}
        if method == 'turn/start':
            thread = params['threadId']
            turn = f'turn-{len(self.calls)}'
            outcome = self.outcomes.pop(0)
            started = {'method': 'turn/started', 'params': {'threadId': thread, 'turn': {'id': turn, 'status': 'inProgress'}}}
            self.pending.append(started)
            self.pending.extend(self._events(outcome, thread, turn))
            return {'turn': {'id': turn, 'status': 'inProgress'}}
        if method == 'account/rateLimits/read':
            if not self.rate_limits:
                raise RuntimeError('account/rateLimits/read failed: unknown method')
            value = self.rate_limits.pop(0) if len(self.rate_limits) > 1 else self.rate_limits[0]
            if isinstance(value, Exception):
                raise value
            return {'rateLimits': value}
        raise AssertionError(method)

    def _goal_event(self, thread, status):
        objective = self.goals[thread]['objective']
        return {'method': 'thread/goal/updated', 'params': {'threadId': thread,
                'goal': {'objective': objective, 'status': status}}}

    def _done(self, thread, turn, status='completed', error=None):
        return {'method': 'turn/completed', 'params': {'threadId': thread,
                'turn': {'id': turn, 'status': status, 'error': error}}}

    def _error(self, thread, turn, error):
        return {'method': 'error', 'params': {'threadId': thread, 'turnId': turn, 'error': error, 'willRetry': False}}

    def _events(self, outcome, thread, turn):
        if outcome == 'complete':
            return [self._goal_event(thread, 'complete'), self._done(thread, turn)]
        if outcome == 'agent_blocked':
            return [self._goal_event(thread, 'blocked'), self._done(thread, turn)]
        if outcome == 'overloaded':
            # Real order (MoVA stage 8): the goal flips to blocked before the turn error arrives.
            return [self._goal_event(thread, 'blocked'), self._error(thread, turn, OVERLOADED),
                    self._done(thread, turn, 'failed', OVERLOADED)]
        if outcome == 'stream503_failed':
            return [self._done(thread, turn, 'failed', STREAM_503)]
        if outcome == 'usage':
            return [self._goal_event(thread, 'usageLimited'), self._error(thread, turn, USAGE),
                    self._done(thread, turn, 'failed', USAGE)]
        raise AssertionError(outcome)

    def read_event(self, log, on_event=None, timeout=None):
        if not self.pending:
            if timeout is not None:
                raise runner.ReadTimeout()
            raise AssertionError('runner waited for an event the script never sends')
        message = self.pending.pop(0)
        if on_event is not None:
            on_event(message)
        return message

    def turn_texts(self):
        return [params['input'][0]['text'] for method, params in self.calls if method == 'turn/start']

    def methods(self):
        return [method for method, _params in self.calls]


@pytest.fixture
def stage(tmp_path, monkeypatch):
    prompt = tmp_path / '03-demo.txt'
    prompt.write_text('/goal Finish the demo stage.\n')
    log_dir = tmp_path / 'run'
    log_dir.mkdir()
    sleeps = []
    monkeypatch.setattr(runner, '_sleep', sleeps.append)
    monkeypatch.setattr(runner, 'TERMINAL_DRAIN_SECONDS', 0.5)

    def make_args(*extra):
        monkeypatch.setattr(sys, 'argv', ['multigoal', str(prompt), '--log-dir', str(log_dir), *extra])
        args = runner.parse_args()
        args._telemetry = None
        return args

    def run(client, *extra, index=3):
        args = make_args(*extra)
        code = runner.run_stage(client, args, tmp_path, log_dir, log_dir / 'manifest.txt', index, prompt, [])
        runner.flush_notifications(5)
        return code

    return {'prompt': prompt, 'log_dir': log_dir, 'sleeps': sleeps, 'run': run, 'args': make_args, 'tmp': tmp_path}


def manifest(stage):
    return runner.read_manifest(stage['log_dir'] / 'manifest.txt')


def write_check(stage, codes):
    """A check script that exits with the next code from `codes` on each call."""
    counter = stage['tmp'] / 'check-count'
    counter.write_text('0')
    script = stage['prompt'].with_name(stage['prompt'].stem + '.check.sh')
    script.write_text(
        'n=$(cat "%s"); echo $((n+1)) > "%s"\ncodes=(%s)\necho "check output line"\nexit ${codes[$n]}\n'
        % (counter, counter, ' '.join(str(c) for c in codes)))
    return counter


# --- classification -------------------------------------------------------

@pytest.mark.parametrize('terminal, error, expected', [
    ('complete', None, 'complete'),
    ('blocked', None, 'other'),
    ('blocked', OVERLOADED, 'transient'),
    ('turnFailed', STREAM_503, 'transient'),
    ('turnFailed', {'message': 'Selected model is at capacity'}, 'transient'),
    ('turnFailed', {'message': 'stream disconnected before completion'}, 'transient'),
    ('turnFailed', {'codexErrorInfo': 'server_overloaded'}, 'transient'),
    ('turnFailed', {'message': 'tool output was invalid', 'codexErrorInfo': 'other'}, 'other'),
    ('usageLimited', None, 'usage'),
    ('budgetLimited', None, 'usage'),
    ('turnFailed', USAGE, 'usage'),
])
def test_classify_stop(terminal, error, expected):
    assert runner.classify_stop(terminal, json.dumps(error) if error else None) == expected


def test_reset_time_from_error_message_and_rate_limits():
    epoch = runner.reset_from_message(USAGE['message'])
    assert time.localtime(epoch)[:5] == (2026, 10, 22, 6, 14)
    snapshot = {'primary': {'usedPercent': 100, 'resetsAt': 1_900_000_000}, 'secondary': {'usedPercent': 40, 'resetsAt': 5}}
    assert runner.usage_reset_epoch(snapshot, json.dumps(USAGE)) == 1_900_000_000
    assert runner.usage_reset_epoch(None, json.dumps(USAGE)) == epoch
    assert runner.usage_reset_epoch(None, None) is None


def test_backoff_and_flag_validation(stage):
    args = stage['args']()
    assert (args.transient_retries, args.transient_backoff_seconds) == (3, [300, 900, 2700])
    assert args.remediation_thread == 'same' and not args.remediate_advisory and args.min_headroom_pct == 15
    with pytest.raises(SystemExit):
        stage['args']('--transient-backoff', '10,abc')
    with pytest.raises(SystemExit, match='requires --resume-stage'):
        stage['args']('--message-file', str(stage['prompt']))


# --- transient errors ------------------------------------------------------

def test_transient_error_resumes_same_thread_with_backoff(stage):
    client = FakeAppServer(['overloaded', 'complete'])
    assert stage['run'](client) == 0
    assert stage['sleeps'] == [300]
    assert client.methods().count('thread/start') == 1
    assert 'thread/resume' not in client.methods()  # same live thread, no reload
    turns = [params['threadId'] for method, params in client.calls if method == 'turn/start']
    assert turns == ['thread-1', 'thread-1']
    assert 'transient API error' in client.turn_texts()[1]
    values = manifest(stage)
    assert values['stage_3_auto_resume_1_reason'] == 'transient'
    assert values['stage_3_terminal_status'] == 'complete'
    status = (stage['log_dir'] / 'STATUS.md').read_text()
    assert 'resume 1/3' in status and '| complete | none |' in status


def test_transient_retries_exhaust_then_stop_and_notify(stage):
    out = stage['tmp'] / 'notify.txt'
    client = FakeAppServer(['stream503_failed'] * 3)
    code = stage['run'](client, '--transient-retries', '2', '--transient-backoff', '5,7',
                        '--notify-cmd', f'echo "$STAGE|$STATUS|$LOG_DIR|$MESSAGE" >> "{out}"')
    assert code == 5
    assert stage['sleeps'] == [5, 7]
    line = out.read_text().strip()
    assert line.startswith(f"03-03-demo|turnFailed|{stage['log_dir']}|Stopping after stage 3: turn failed")


def test_agent_declared_blocker_is_not_retried(stage):
    client = FakeAppServer(['agent_blocked'])
    assert stage['run'](client) == 3
    assert stage['sleeps'] == []
    assert manifest(stage)['stage_3_terminal_status'] == 'blocked'


# --- usage limits ----------------------------------------------------------

def test_usage_limit_without_wait_flag_stops_with_reset_in_status(stage):
    client = FakeAppServer(['usage'])
    assert stage['run'](client, '--min-headroom-pct', '0') == 3
    status = (stage['log_dir'] / 'STATUS.md').read_text()
    assert 'usage limit; resets 2026-10-22T' in status and '--wait-on-usage-limit' in status
    assert stage['sleeps'] == []


def test_usage_limit_waits_until_reset_and_resumes(stage):
    reset = time.time() + 3600
    client = FakeAppServer(['usage', 'complete'], rate_limits=[{'primary': {'usedPercent': 100, 'resetsAt': reset}}])
    assert stage['run'](client, '--wait-on-usage-limit') == 0
    assert len(stage['sleeps']) == 1 and 3600 < stage['sleeps'][0] < 3800
    assert manifest(stage)['stage_3_auto_resume_1_reason'] == 'usage'


def test_usage_wait_budget_is_bounded(stage):
    reset = time.time() + 10 * 3600
    client = FakeAppServer(['usage'], rate_limits=[{'primary': {'usedPercent': 100, 'resetsAt': reset}}])
    assert stage['run'](client, '--wait-on-usage-limit', '--max-usage-wait-hours', '0.5') == 3
    assert stage['sleeps'] == []
    assert 'wait budget exhausted' in (stage['log_dir'] / 'STATUS.md').read_text()


def test_headroom_pause_between_stages(stage):
    reset = time.time() + 1000
    client = FakeAppServer([], rate_limits=[{'primary': {'usedPercent': 90, 'resetsAt': reset}},
                                            {'primary': {'usedPercent': 2, 'resetsAt': reset + 9999}}])
    args = stage['args']()
    runner.wait_for_headroom(client, args, stage['log_dir'], '04-next')
    assert len(stage['sleeps']) == 1 and 1000 < stage['sleeps'][0] < 1200
    assert 'paused before start: 10% rate-limit headroom' in (stage['log_dir'] / 'STATUS.md').read_text()


@pytest.mark.parametrize('limits', [
    [RuntimeError('method not found')],
    [{'primary': {'usedPercent': 99, 'resetsAt': 5}, 'credits': {'hasCredits': True, 'balance': '12.5'}}],
    [{'primary': {'usedPercent': 50, 'resetsAt': 5}}],
])
def test_headroom_degrades_gracefully(stage, limits):
    runner.wait_for_headroom(FakeAppServer([], rate_limits=limits), stage['args'](), stage['log_dir'], '04-next')
    assert stage['sleeps'] == []


# --- steering --------------------------------------------------------------

def test_resume_stage_with_message_file_sends_owner_text(stage):
    log_dir = stage['log_dir']
    (log_dir / 'manifest.txt').write_text('stage_3_thread_id=thread-old\nstage_3_terminal_status=blocked\n')
    steer = stage['tmp'] / 'steer.md'
    steer.write_text('Run an HF control at the same settings, then accept the limitation if HF matches.\n')
    args = stage['args']('--resume-stage', '3', '--message-file', str(steer))
    client = FakeAppServer(['complete'])
    code = runner.run_resumed_stage(client, args, stage['tmp'], log_dir, log_dir / 'manifest.txt', 3,
                                    stage['prompt'], [], runner.read_manifest(log_dir / 'manifest.txt'))
    assert code == 0
    assert client.methods()[0] == 'thread/resume'
    text = client.turn_texts()[0]
    assert text.startswith('Run an HF control at the same settings')
    assert 'Continue this existing multigoal stage' not in text
    values = manifest(stage)
    assert values['stage_3_resume_1_message_file'].endswith('03-demo.resume-1.message.txt')
    assert values['stage_3_resume_1_terminal_status'] == 'complete'


# --- checks and remediation ------------------------------------------------

def test_advisory_check_is_recorded_without_remediation(stage):
    counter = write_check(stage, [1])
    client = FakeAppServer(['complete'])
    assert stage['run'](client) == 0
    assert counter.read_text().strip() == '1'
    assert len(client.turn_texts()) == 1
    assert manifest(stage)['stage_3_check'] == 'advisory-fail'


def test_remediate_advisory_restores_old_behaviour(stage):
    write_check(stage, [1, 0])
    client = FakeAppServer(['complete', 'complete'])
    assert stage['run'](client, '--remediate-advisory') == 0
    assert len(client.turn_texts()) == 2
    assert manifest(stage)['stage_3_check'] == 'pass'


def test_remediation_runs_in_the_same_thread_by_default(stage):
    write_check(stage, [2, 0])
    client = FakeAppServer(['complete', 'complete'])
    assert stage['run'](client) == 0
    assert client.methods().count('thread/start') == 1
    assert 'thread/resume' not in client.methods()
    assert 'Runner-side verification' in client.turn_texts()[1]
    # The stage goal objective is kept; the check report is the turn text.
    assert client.goals['thread-1']['objective'] == 'Finish the demo stage.'
    values = manifest(stage)
    assert values['stage_3_remediation_1_thread_mode'] == 'same'
    assert values['stage_3_remediation_1_thread_id'] == 'thread-1'
    assert values['stage_3_check'] == 'pass'


def test_remediation_thread_new_starts_a_fresh_goal(stage):
    write_check(stage, [2, 2])
    out = stage['tmp'] / 'notify.txt'
    client = FakeAppServer(['complete', 'complete'])
    code = stage['run'](client, '--remediation-thread', 'new', '--notify-cmd', f'echo "$STATUS" >> "{out}"')
    assert code == 6
    assert client.methods().count('thread/start') == 2
    assert manifest(stage)['stage_3_remediation_1_thread_id'] == 'thread-2'
    assert out.read_text().split() == ['critical-fail']


# --- notify and the real client's timed reads ---------------------------------

def test_notify_timeout_does_not_block(stage):
    args = stage['args']('--notify-cmd', 'sleep 30', '--notify-timeout', '1')
    start = time.monotonic()
    thread = runner.notify(args, 'run', 'complete', stage['log_dir'], 'done')
    assert time.monotonic() - start < 1
    thread.join(10)
    assert not thread.is_alive()


def test_app_server_client_timed_read(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'RATE_LIMIT_READ_TIMEOUT', 0.3)
    fake = tmp_path / 'fake-codex'
    fake.write_text(
        f'#!{sys.executable}\n'
        'import json, sys\n'
        'for line in sys.stdin:\n'
        '    msg = json.loads(line)\n'
        '    if msg.get("method") == "initialize":\n'
        '        print(json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 7}}}}), flush=True)\n'
        '        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = (tmp_path / 'log.jsonl').open('w')
    with runner.AppServerClient(str(fake), tmp_path, dict(os.environ), tmp_path / 'err.log', []) as client:
        assert client.request('initialize', {}, log, timeout=10) == {}
        assert client.latest_rate_limits == {'primary': {'usedPercent': 7}}
        with pytest.raises(runner.ReadTimeout):
            client.request('account/rateLimits/read', None, log, timeout=0.3)
        with pytest.raises(runner.ReadTimeout):
            client.read_event(log, timeout=0.2)
        snapshot = runner.read_rate_limits(client, tmp_path)
    assert snapshot == {'primary': {'usedPercent': 7}}
