"""wait_for: one blocking call instead of sleep/tail polling loops."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
WAIT_FOR = REPO / 'plugins/tt-model-bringup/scripts/wait_for'


def wait_for(*args, timeout=30):
    return subprocess.run([sys.executable, str(WAIT_FOR), *args], text=True, capture_output=True, timeout=timeout)


def test_is_executable_python():
    assert os.access(WAIT_FOR, os.X_OK)
    assert WAIT_FOR.read_text().startswith('#!/usr/bin/env python3')


def test_cmd_success_failure_and_tail_cap():
    ok = wait_for('--cmd', 'for i in $(seq 1 100); do echo line $i; done', '--tail', '5')
    assert ok.returncode == 0
    lines = ok.stdout.splitlines()
    assert lines[0].startswith('wait_for OK: command exited 0')
    assert lines[-5:] == [f'line {i}' for i in range(96, 101)]
    assert len(lines) == 7
    bad = wait_for('--cmd', 'echo boom; exit 3')
    assert bad.returncode == 1
    assert 'command exited 3' in bad.stdout and 'boom' in bad.stdout
    assert 'full command output kept at' in bad.stdout


def test_cmd_timeout_kills_and_exits_124():
    start = time.monotonic()
    result = wait_for('--cmd', 'echo started; sleep 60', '--timeout', '1', '--interval', '0.2')
    assert result.returncode == 124
    assert time.monotonic() - start < 20
    assert result.stdout.startswith('wait_for TIMEOUT') and 'started' in result.stdout


def test_pid_exit_and_timeout():
    proc = subprocess.Popen(['sleep', '1'])
    result = wait_for('--pid', str(proc.pid), '--interval', '0.2')
    proc.wait()
    assert result.returncode == 0 and 'exited' in result.stdout
    sleeper = subprocess.Popen(['sleep', '30'])
    try:
        result = wait_for('--pid', str(sleeper.pid), '--timeout', '0.5', '--interval', '0.1')
        assert result.returncode == 124 and 'still running' in result.stdout
    finally:
        sleeper.kill()
        sleeper.wait()


def test_pattern_in_growing_log_and_line_clip(tmp_path):
    log = tmp_path / 'server.log'
    writer = subprocess.Popen(['bash', '-c', f'sleep 0.5; echo "{"x" * 1000}" >> {log}; echo "INFO: Application startup complete" >> {log}; sleep 30'])
    try:
        result = wait_for('--file', str(log), '--pattern', 'startup complete', '--interval', '0.1', '--tail', '10')
    finally:
        writer.kill()
        writer.wait()
    assert result.returncode == 0
    assert result.stdout.startswith('wait_for OK: pattern matched')
    assert max(len(line) for line in result.stdout.splitlines()) <= 400


def test_fail_pattern_and_new_only(tmp_path):
    log = tmp_path / 'run.log'
    log.write_text('old: ready\n')
    assert wait_for('--file', str(log), '--pattern', 'ready').returncode == 0
    result = wait_for('--file', str(log), '--pattern', 'ready', '--new-only', '--timeout', '0.6', '--interval', '0.1')
    assert result.returncode == 124
    log.write_text('step 1\nTT_FATAL: device timeout\n')
    result = wait_for('--file', str(log), '--pattern', 'ready', '--fail-pattern', 'TT_FATAL|Traceback')
    assert result.returncode == 1 and 'fail-pattern matched' in result.stdout


def test_cmd_with_file_and_pattern_stops_early(tmp_path):
    log = tmp_path / 'job.log'
    result = wait_for('--cmd', 'echo warming; echo READY; sleep 30', '--file', str(log), '--pattern', 'READY',
                      '--interval', '0.1')
    assert result.returncode == 0 and 'pattern matched' in result.stdout
    pid = int(result.stdout.split('command still running, pid ')[1].split(',')[0])
    os.killpg(pid, 9)


def test_usage_errors():
    assert wait_for().returncode == 2
    assert wait_for('--pattern', 'x').returncode == 2
    assert wait_for('--pid', '1', '--cmd', 'true').returncode == 2
