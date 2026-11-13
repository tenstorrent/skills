"""Device held/idle timeline: a fake /proc, exact totals, no device access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
sys.path.insert(0, str(PLUGIN / 'scripts'))
import device_timeline as dtl  # noqa: E402

RUNNER = 100


class FakeHost:
    """A fake /proc and /dev/tenstorrent. Links to devices point at paths that do not exist."""

    def __init__(self, root: Path, devices: int = 3):
        self.proc, self.dev = root / 'proc', root / 'dev'
        self.proc.mkdir()
        self.dev.mkdir()
        for number in range(devices):
            (self.dev / str(number)).write_text('')
        self.process(1, 0, 'init')
        self.process(RUNNER, 1, 'multigoal --repo x')

    def process(self, pid, ppid, cmdline, devices=(), unreadable=False, other_fd='/tmp/log'):
        base = self.proc / str(pid)
        base.mkdir(exist_ok=True)
        (base / 'stat').write_text(f'{pid} (my proc) S {ppid} 0 0 0\n')
        (base / 'cmdline').write_bytes(cmdline.replace(' ', '\0').encode())
        fd = base / 'fd'
        if unreadable:
            fd.write_text('permission denied stand-in: not a directory')
            return
        fd.mkdir(exist_ok=True)
        (fd / '0').symlink_to(other_fd)
        for number in devices:
            (fd / str(10 + number)).symlink_to(f'/dev/tenstorrent/{number}')

    def gone(self, pid):
        import shutil
        shutil.rmtree(self.proc / str(pid))


def timeline(host, path, clock, fresh=True):
    return dtl.DeviceTimeline(path, fresh=fresh, root_pid=RUNNER, proc=host.proc, dev_dir=host.dev,
                              interval=15, clock=lambda: clock[0])


def at(clock, tl, t, event=None):
    clock[0] = t
    tl.tick(event)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def run(tmp_path):
    host = FakeHost(tmp_path)
    path = tmp_path / 'doc/device_timeline.jsonl'
    clock = [1000]
    tl = timeline(host, path, clock)
    tl.start()                                             # the stage_start line; stop the thread, tick by hand
    tl.stop_event.set()
    at(clock, tl, 1015)                                    # no change: no line
    host.process(200, RUNNER, 'python3 server.py --port 8000', devices=[0])   # the stage's own holder
    host.process(300, 1, 'bash', devices=[])               # not a holder
    at(clock, tl, 1030)
    host.process(400, 1, 'python3 /other/job.py ' + 'x' * 300, devices=[1, 2])  # someone else, more devices
    at(clock, tl, 1045)                                    # held 3, first holder is still the stage's
    at(clock, tl, 1060)                                    # no change
    host.gone(200)
    at(clock, tl, 1090)                                    # only the other holder: held 2, other
    host.gone(400)
    host.process(500, 1, 'unreadable', unreadable=True)    # fds cannot be read: unknown, not free
    at(clock, tl, 1120)
    host.gone(500)
    at(clock, tl, 1150)                                    # idle
    at(clock, tl, 1200, 'stage_end')
    return host, path, clock, tl


def test_only_changes_are_logged_with_the_fields(run):
    _host, path, _clock, _tl = run
    got = lines(path)
    assert [(item['t'], item['held'], item['of'], item['other'], item.get('event')) for item in got] == [
        (1000, 0, 3, False, 'stage_start'), (1030, 1, 3, False, None), (1045, 3, 3, False, None),
        (1090, 2, 3, True, None), (1120, None, 3, False, None), (1150, 0, 3, False, None),
        (1200, 0, 3, False, 'stage_end')]
    assert got[1]['by'] == 'python3 server.py --port 8000'
    assert got[3]['by'].startswith('python3 /other/job.py') and len(got[3]['by']) == dtl.BY_CHARS
    assert got[4]['unreadable'] == 1 and 'unreadable' not in got[1]
    assert got[0]['uid'] == os.getuid() and got[0]['runner_pid'] == RUNNER and got[0]['host']


def test_totals_are_exact(run):
    _host, path, _clock, _tl = run
    summary = dtl.summarize(path)
    assert (summary['held_s'], summary['held_other_s'], summary['idle_s'], summary['unknown_s'],
            summary['wall_s']) == (60, 30, 80, 30, 200)
    assert summary['devices'] == 3 and summary['complete'] is False   # an unreadable interval
    assert dtl.summarize(path, end=1260)['idle_s'] == 140             # the last state runs to the end


def test_runner_restart_gap_is_unknown(run):
    host, path, clock, tl = run
    at(clock, tl, 1230)                                    # last tick of the first runner, no line
    clock[0] = 1500
    again = timeline(host, path, clock, fresh=False)
    again.start()
    again.stop_event.set()
    restart = lines(path)[-1]
    assert restart['event'] == 'runner_start' and restart['prev_alive'] == 1230
    clock[0] = 1560
    again.tick('stage_end')
    summary = dtl.summarize(path)
    assert (summary['held_s'], summary['held_other_s'], summary['idle_s'], summary['unknown_s']) == (
        60, 30, 80 + 30 + 60, 30 + 270)
    assert summary['wall_s'] == 560 == 60 + 30 + 170 + 300
    # A quick restart (gap within 2 intervals) is not unknown; no known last tick is all unknown.
    quick = path.parent / 'quick.jsonl'
    quick.write_text('\n'.join(json.dumps(item) for item in [
        {'t': 0, 'held': 1, 'of': 1, 'by': 'a', 'other': False, 'event': 'stage_start', 'interval': 15},
        {'t': 100, 'held': 1, 'of': 1, 'by': 'a', 'other': False, 'event': 'runner_start', 'prev_alive': 90},
        {'t': 200, 'held': 1, 'of': 1, 'by': 'a', 'other': False, 'event': 'runner_start'},
        {'t': 230, 'held': 0, 'of': 1, 'by': '', 'other': False, 'event': 'stage_end'}]) + '\n')
    result = dtl.summarize(quick)
    assert (result['held_s'], result['unknown_s'], result['idle_s'], result['wall_s']) == (130, 100, 0, 230)


def test_timeline_resets_at_the_last_stage_start_and_missing_file_is_none(run, tmp_path):
    _host, path, _clock, _tl = run
    with path.open('a') as handle:
        handle.write(json.dumps({'t': 5000, 'held': 0, 'of': 3, 'by': '', 'other': False, 'event': 'stage_start'}) + '\n')
        handle.write(json.dumps({'t': 5100, 'held': 1, 'of': 3, 'by': 'x', 'other': False, 'event': 'stage_end'}) + '\n')
    assert dtl.summarize(path)['wall_s'] == 100 and dtl.summarize(path)['idle_s'] == 100
    assert dtl.summarize(tmp_path / 'nothing.jsonl') is None


def test_background_thread_logs_start_and_end_without_touching_devices(tmp_path):
    host = FakeHost(tmp_path)
    host.process(200, RUNNER, 'server', devices=[0])
    path = tmp_path / 'timeline.jsonl'
    tl = dtl.DeviceTimeline(path, fresh=True, root_pid=RUNNER, proc=host.proc, dev_dir=host.dev, interval=0.01)
    tl.start()
    time.sleep(0.15)
    tl.stop()
    got = lines(path)
    assert [item.get('event') for item in got] == ['stage_start', 'stage_end']   # steady state: no other lines
    assert got[0]['held'] == 1 and (path.parent / dtl.ALIVE).is_file()
    assert not tl.thread.is_alive()
    source = Path(dtl.__file__).read_text()
    assert 'tt-smi' not in source.replace('never calls tt-smi', '') and 'open(DEVICE' not in source


def test_other_users_processes_are_hidden_not_unknown(tmp_path, monkeypatch):
    """A non-root runner cannot read other users' fds (on a real host, hundreds of root processes): they are
    counted as hidden and do not turn idle into unknown. Our own unreadable process still does."""
    host = FakeHost(tmp_path)
    host.process(600, 1, 'root daemon', unreadable=True)
    me = os.getuid()
    monkeypatch.setattr(dtl, '_owner', lambda proc, pid: me + 1 if pid == 600 else me)
    seen = dtl.scan(host.proc, host.dev, RUNNER)
    assert seen['held'] == 0 and seen['hidden'] == 1 and 'unreadable' not in seen
    monkeypatch.setattr(dtl, '_owner', lambda proc, pid: me)
    assert dtl.scan(host.proc, host.dev, RUNNER)['held'] is None


def test_background_server_reparented_to_init_is_the_stages(tmp_path):
    """An agent starts the server in the background; its shell exits and the server's parent becomes init.
    Started after the stage began by the runner's user, it is still the stage's holder, not other."""
    host = FakeHost(tmp_path)
    tick = os.sysconf('SC_CLK_TCK')
    (host.proc / 'stat').write_text('cpu 0 0 0\nbtime 1000\n')

    def started(pid, at):
        rest = ' '.join(['0'] * 17)                       # pgrp .. itrealvalue; starttime is index 19
        (host.proc / str(pid) / 'stat').write_text(f'{pid} (srv) S 1 {rest} {int((at - 1000) * tick)} 0\n')

    host.process(700, 1, 'vllm serve', devices=[0])
    started(700, 2000)
    assert dtl.scan(host.proc, host.dev, RUNNER, since=1500)['other'] is False
    assert dtl.scan(host.proc, host.dev, RUNNER, since=2500)['other'] is True    # started before the stage
    assert dtl.scan(host.proc, host.dev, RUNNER)['other'] is True               # no stage start known


def test_report_row_says_how_many_processes_were_hidden():
    import stage_usage
    row = stage_usage.device_line({'timeline': dtl.TIMELINE, 'held_s': 3600, 'idle_s': 0, 'unknown_s': 0,
                                   'held_other_s': 0, 'wall_s': 3600, 'complete': True, 'hidden': 919})
    assert 'up to 919 processes of other users were not visible' in row


def test_zombies_are_skipped(tmp_path):
    """A zombie's fd dir is unreadable (EACCES) but it holds no files: neither unknown nor hidden."""
    host = FakeHost(tmp_path)
    host.process(800, 1, 'gh', unreadable=True)
    (host.proc / '800' / 'stat').write_text('800 (gh) Z 1 0 0 0\n')
    seen = dtl.scan(host.proc, host.dev, RUNNER)
    assert seen['held'] == 0 and 'unreadable' not in seen and 'hidden' not in seen
