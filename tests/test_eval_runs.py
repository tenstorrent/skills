"""eval_runs.py: detached runs survive the caller, wait blocks without model calls, runs.json records the end."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/scripts/eval_runs.py'


def call(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def runs(model_dir):
    return {row['id']: row for row in json.loads((model_dir / 'doc/agentic_evals/runs.json').read_text())['runs']}


def test_start_wait_records_exit_and_hours(tmp_path):
    started = call('start', '--model-dir', str(tmp_path), '--id', 'gpqa-r1', '--', 'bash', '-c', 'sleep 0.3; exit 3')
    assert started.returncode == 0, started.stderr
    assert 'conclusion' not in runs(tmp_path)['gpqa-r1']
    waited = call('wait', '--model-dir', str(tmp_path), '--minutes', '1', '--look-s', '0.1')
    assert waited.returncode == 0, waited.stderr
    row = runs(tmp_path)['gpqa-r1']
    assert row['conclusion'] == 'exit 3' and row['exit_code'] == 3 and row['hours'] >= 0 and row['end_utc']
    assert json.loads(waited.stdout.splitlines()[0])['conclusion'] == 'exit 3'


def test_wait_returns_1_when_time_ends_and_never_stops_the_run(tmp_path):
    call('start', '--model-dir', str(tmp_path), '--id', 'slow', '--', 'sleep', '5')
    t0 = time.monotonic()
    waited = call('wait', '--model-dir', str(tmp_path), '--id', 'slow', '--minutes', '0.005', '--look-s', '0.05')
    assert waited.returncode == 1 and time.monotonic() - t0 < 4
    pid = runs(tmp_path)['slow']['pid']
    os.kill(pid, 0)                                         # still running
    os.killpg(pid, 9)


def test_a_run_gone_without_exit_code_is_lost_and_ids_are_unique(tmp_path):
    call('start', '--model-dir', str(tmp_path), '--id', 'w1', '--', 'sleep', '30')
    pid = runs(tmp_path)['w1']['pid']
    os.killpg(pid, 9)                                       # the wrapper dies before writing the exit code
    time.sleep(0.2)
    call('wait', '--model-dir', str(tmp_path), '--id', 'w1', '--minutes', '0.05', '--look-s', '0.05')
    assert runs(tmp_path)['w1']['conclusion'] == 'lost'
    again = call('start', '--model-dir', str(tmp_path), '--id', 'w1', '--', 'true')
    assert again.returncode != 0 and 'already' in again.stderr
