from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'plugins/tt-model-bringup/scripts/reply_digest.py'
FIXTURE = ROOT / 'tests/fixtures/reply_digest/gemma4_v4_tails.json'
sys.path.insert(0, str(SCRIPT.parent))

import reply_digest  # noqa: E402


def body(rid, text, finish='stop', tokens=100, prompt=1000, tool=None):
    message = {'role': 'assistant', 'content': text}
    if tool:
        message['tool_calls'] = [{'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({'command': tool})}}]
    return {'id': rid, 'object': 'chat.completion', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': tokens, 'total_tokens': prompt + tokens}}


def plain_line(b):
    return f'INFO 11-13 08:57:01 [log_response] response_body={{{json.dumps(b)}}}\n'


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def fixture_replies():
    return json.loads(FIXTURE.read_text())['replies']


def test_gemma_fixture_flags_18_of_19_capped_and_no_normal_reply():
    replies = fixture_replies()
    rows = {r['id']: reply_digest.reply_row(body(r['id'], r['tail'], r['finish'], r['tokens'])) for r in replies}
    capped = [r for r in replies if r['kind'] == 'capped']
    normal = [r for r in replies if r['kind'] == 'normal']
    assert len(capped) == 19 and len(normal) >= 30
    assert sum(rows[r['id']]['flag'] for r in capped) == 18
    assert not rows['90e29278']['flag']  # the garbled-phrase loop: a triage sub-agent's case
    assert not any(rows[r['id']]['flag'] for r in normal)
    for r in replies:
        assert rows[r['id']]['flag'] is r['expected_flag'], r['id']


def test_fixture_holds_no_gpqa_text():
    for r in fixture_replies():
        if (r['run'] or '').startswith('gpqa'):
            assert r['kind'] == 'capped' and len(set(r['tail'])) <= 10


def test_capped_at_the_output_cap_whatever_the_finish_reason():
    b = body('x', 'a' * 2000, finish='tool_calls', tokens=32768)
    assert not reply_digest.reply_row(b)['capped']
    assert reply_digest.reply_row(b, cap=32768)['capped']


def test_short_replies_are_never_flagged():
    assert not reply_digest.reply_row(body('x', 'OK.'))['flag']


def test_hex_framed_records_are_read(tmp_path):
    first = json.dumps({'version': 1, 'name': 'log_response', 'message': 'response_body={' + json.dumps(body('a', 'hi')) + '}'})
    second = json.dumps({'version': 1, 'name': 'log_response',
                         'message': 'response_body={' + json.dumps(body('b', 'z' * 3000, 'length')) + '}'}).encode()
    digest, half = hashlib.sha256(second).hexdigest(), len(second) // 2
    log = tmp_path / 'server.log'
    log.write_text(f'x TTNATIVEHEX1 {first.encode().hex()}\n'
                   f'x TTNATIVEHEX2 {digest} 1 2 {len(second)} {second[half:].hex()}\n'
                   f'x TTNATIVEHEX2 {digest} 0 2 {len(second)} {second[:half].hex()}\n')
    rows = reply_digest.read_replies([log])
    assert set(rows) == {'a', 'b'} and rows['b']['flag'] and rows['b']['capped']


def write_run(tmp_path):
    loop = ''.join(f'Step {i}: run test_{i * 7 % 13}.py again, then patch line {i * 31 % 97}. ' for i in range(120))
    bodies = [body('c1', 'thinking ' * 50, 'tool_calls', 40, tool='ls -la'),
              body('c2', 'more ' * 40, 'tool_calls', 40, tool='ls -la'),
              body('c3', "'s" * 3000, 'length', 32768),
              body('d1', 'ok', 'stop', 5),
              body('d2', loop, 'length', 32768),
              body('stray', 'x' * 2000, 'length', 32768)]
    log = tmp_path / 'server.log'
    log.write_text('noise\n' + ''.join(plain_line(b) for b in bodies))
    accounting = tmp_path / 'collected.json'
    accounting.write_text(json.dumps({
        'trials': [{'task': 'repo__a-1', 'calls': [{'id': 'c1'}, {'id': 'c2'}, {'id': 'c3'}]}],
        'calls_by_task': [{'doc_id': 7, 'response_id': 'd1'}, {'doc_id': 9, 'response_id': None}],
        'extra': {'tasks': {'swe/b-2': ['d2', 'missing-id']}}}))
    return log, accounting


def test_digest_per_task_and_summary(tmp_path):
    log, accounting = write_run(tmp_path)
    out = tmp_path / 'doc/agentic_evals/current/swe-run/digest'
    result = run('--log', log, '--accounting', accounting, '--out', out)
    assert result.returncode == 0, result.stderr
    assert '| repo__a-1 | 3 | 1 | 1 |' in result.stdout
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['run'] == 'swe-run' and summary['replies'] == 6
    assert set(summary['tasks']) == {'repo__a-1', '7', '9', 'swe/b-2'}
    assert summary['tasks']['9']['calls'] == 0
    assert summary['tasks']['swe/b-2'] == {**summary['tasks']['swe/b-2'], 'capped': 1, 'flagged': 0, 'digest': 'swe_b-2.json'}
    assert summary['missing_ids'] == {'swe/b-2': ['missing-id']}
    assert summary['unmapped']['capped'] == 1 and summary['unmapped']['capped_or_flagged'][0]['id'] == 'stray'
    text = (out / 'repo__a-1.json').read_text()
    digest = json.loads(text)
    assert [c['n'] for c in digest['calls']] == [1, 2, 3]
    assert digest['calls'][1]['same_as'] == 1 and digest['calls'][0]['action'].startswith('tool bash')
    assert [t['n'] for t in digest['tails']] == [3] and len(digest['tails'][0]['tail']) == reply_digest.TAIL_KEEP_CHARS
    assert all(line.strip().startswith('{"id"') for line in text.splitlines()[1:4])  # one line per call
    assert digest['approx_tokens'] < 30000
    loop_digest = json.loads((out / 'swe_b-2.json').read_text())
    assert loop_digest['tails'] and not loop_digest['calls'][0]['flag']  # capped but unflagged: tail kept for triage


def test_logging_check(tmp_path):
    log, _ = write_run(tmp_path)
    record = tmp_path / 'doc/agentic_evals/logging_check.json'
    result = run('--check-log', '--log', log, '--id', 'd1', '--record', record)
    assert result.returncode == 0, result.stdout
    saved = json.loads(record.read_text())
    assert saved['pass'] and saved['id'] == 'd1' and saved['tokens'] == 5 and saved['finish'] == 'stop'
    bare = body('e1', 'hello', None)
    del bare['usage']
    log.write_text(plain_line(bare))
    result = run('--check-log', '--log', log, '--record', record)
    assert result.returncode == 1
    saved = json.loads(record.read_text())
    assert not saved['pass'] and len(saved['problems']) == 2
    log.write_text('INFO request logging only\n')
    assert run('--check-log', '--log', log).returncode == 1


def test_a_hex_record_cut_off_mid_write_is_skipped_not_a_crash():
    """A server restart can leave a truncated (odd-length) hex record in the log; the digest must still be written."""
    good = json.dumps({'message': 'plain'}).encode().hex()
    lines = [f'x TTNATIVEHEX1 {good}', 'x TTNATIVEHEX1 7b2', f'y TTNATIVEHEX2 {"0" * 64} 0 2 10 abc']
    assert list(reply_digest.framed(lines)) == ['plain', '']
