"""P4 (long answers + health) and P5 (long context) against a fake OpenAI server with fake /metrics."""
from __future__ import annotations

import ast
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins/tt-model-bringup'
sys.path.insert(0, str(PLUGIN / 'runtime'))
sys.path.insert(0, str(PLUGIN / 'scripts'))

from readiness_check import served_probe as sp  # noqa: E402
from readiness_check import run_long_answer_load as p4  # noqa: E402
from readiness_check import run_long_context as p5  # noqa: E402
import check_eval_readiness as gate  # noqa: E402

NEEDLE_RE = re.compile(r'passphrase for the lighthouse keeper is ([a-z]+-\d+)')


class FakeVllm:
    """A tiny vLLM look-alike: streaming chat/completions, /tokenize, /metrics, /health, /v1/models."""

    def __init__(self, tmp_path: Path, **cfg):
        self.cfg = dict(tpot_s=0.0005, host_factor=1.0, fail_every=0, leak_running=0, kv_leak=0.0,
                        capture_during_load=False, recall_ok=True, max_model_len=200000, cliff_at=None,
                        cliff_tpot_s=0.01, misaligned_slow=False, preempt_per_request=0, pool_tokens=10_000_000,
                        sample_mode='all', mnbt=2048, log_line_during_load='', context_error=False)
        self.cfg.update(cfg)
        self.active = 0
        self.requests = 0
        self.preemptions = 0
        self.lock = threading.Lock()
        self.log = tmp_path / 'server.log'
        self.log.write_text(
            f"INFO non-default args: {{'max_num_batched_tokens': {self.cfg['mnbt']}}}\n"
            f"INFO TTModelRunner: trace_mode=all, sample_on_device_mode={self.cfg['sample_mode']}\n"
            "INFO generator: begin_trace_capture decode b32\n")
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/health':
                    return self._json(200, {})
                if self.path == '/v1/models':
                    return self._json(200, {'data': [{'id': 'org/model', 'max_model_len': fake.cfg['max_model_len']}]})
                if self.path == '/metrics':
                    text = fake.metrics().encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(text)))
                    self.end_headers()
                    return self.wfile.write(text)
                self._json(404, {})

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/reset_prefix_cache':
                    return self._json(200, {})
                if self.path == '/tokenize':
                    if 'messages' in body:
                        n = sum(len(m['content']) for m in body['messages']) // 4 + 8
                    else:
                        n = len(body.get('prompt') or '') // 4
                    return self._json(200, {'tokens': list(range(n)), 'count': n})
                if self.path in ('/v1/chat/completions', '/v1/completions'):
                    return fake.stream(self, body, chat=self.path.endswith('chat/completions'))
                self._json(404, {})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def metrics(self) -> str:
        c = self.cfg
        running = self.active + (c['leak_running'] if self.requests else 0)
        kv = self.active * 0.01 + (c['kv_leak'] if self.requests else 0.0)
        return (
            '# HELP vllm:num_requests_running x\n'
            f'vllm:num_requests_running{{model_name="org/model"}} {running}\n'
            f'vllm:num_requests_waiting{{model_name="org/model"}} 0\n'
            f'vllm:kv_cache_usage_perc{{model_name="org/model"}} {kv}\n'
            f'vllm:num_preemptions_total{{model_name="org/model"}} {self.preemptions}\n'
            f'vllm:generation_tokens_total{{model_name="org/model"}} 0\n'
            f'vllm:cache_config_info{{block_size="64",num_gpu_blocks="{c["pool_tokens"] // 64}"}} 1\n')

    def stream(self, handler, body, chat):
        c = self.cfg
        with self.lock:
            self.requests += 1
            index = self.requests
            self.active += 1
            self.preemptions += c['preempt_per_request']
        try:
            if c['fail_every'] and index % c['fail_every'] == 0:
                return handler._json(500, {'error': 'engine failure'})
            if c['context_error'] and chat:
                return handler._json(400, {'error': {'message': "This model's maximum context length is 1200 tokens. "
                                                     'However, you requested 1300 tokens.'}})
            if chat:
                prompt = ' '.join(m['content'] for m in body['messages'])
                plen = len(prompt) // 4
            else:
                prompt = body['prompt']
                plen = len(prompt) if isinstance(prompt, list) else len(prompt) // 4
            needle = NEEDLE_RE.search(prompt) if isinstance(prompt, str) else None
            ttft = 0.002 + plen * 0.00002
            if c['misaligned_slow'] and plen % 128:
                ttft *= 3
            time.sleep(ttft)
            if c['capture_during_load'] or c['log_line_during_load']:
                with open(self.log, 'a') as f:
                    f.write(c['log_line_during_load'] or 'INFO generator: begin_trace_capture prefill 4096\n')
            tpot = c['tpot_s']
            if body.get('top_k') or body.get('seed') is not None:
                tpot *= c['host_factor']
            if c['cliff_at'] and plen >= c['cliff_at']:
                tpot = c['cliff_tpot_s']
            if needle:
                answer = needle.group(1) if c['recall_ok'] else 'I do not know'
                pieces = [answer]
            else:
                pieces = ['tok '] * int(body['max_tokens'])
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.end_headers()
            for i, piece in enumerate(pieces):
                if i:
                    time.sleep(tpot)
                choice = {'index': 0, 'delta': {'content': piece}} if chat else {'index': 0, 'text': piece}
                handler.wfile.write(f'data: {json.dumps({"choices": [choice]})}\n\n'.encode())
                handler.wfile.flush()
            last = {'index': 0, 'delta': {}, 'finish_reason': 'stop' if needle else 'length'} if chat else \
                {'index': 0, 'text': '', 'finish_reason': 'length'}
            handler.wfile.write(f'data: {json.dumps({"choices": [last]})}\n\n'.encode())
            usage = {'prompt_tokens': plen, 'completion_tokens': len(pieces)}
            handler.wfile.write(f'data: {json.dumps({"choices": [], "usage": usage})}\n\ndata: [DONE]\n\n'.encode())
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.lock:
                self.active -= 1

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake(tmp_path):
    servers = []

    def make(**cfg):
        server = FakeVllm(tmp_path, **cfg)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def plan_file(tmp_path, cap=18, tokens=40, questions=10):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps({'note': 'test', 'ci_cap_hours': {'job': cap, 'nightly': cap}, 'fit_fraction': 0.8,
                                'concurrency': 4, 'evals': [{'name': 'gpqa', 'questions': questions,
                                                             'typical_answer_tokens': tokens}]}))
    return path


def p4_args(server, model_dir, tmp_path, *extra, users=4, tokens=40):
    return ['--server-url', server.url, '--model-dir', str(model_dir), '--server-log', str(server.log),
            '--users', str(users), '--max-tokens', str(tokens), '--bucket-tokens', '10', '--compare-tokens', '12',
            '--metrics-interval', '0.05', '--settle-timeout', '1', '--functional-floor-tps', '1',
            '--eval-plan', str(plan_file(tmp_path)), '--profiles-file', str(tmp_path / 'none.json'), *extra]


def result(model_dir, probe):
    data = json.loads((model_dir / 'doc/eval_readiness' / f'{probe}.json').read_text())
    return data, {row['id']: row for row in data['checks']}


# ------------------------------------------------------------------ P4


def test_p4_healthy_server_passes(fake, tmp_path, capsys):
    server = fake()
    model = tmp_path / 'model'
    assert p4.main(p4_args(server, model, tmp_path)) == 0, capsys.readouterr().out
    data, checks = result(model, 'long_answer_load')
    assert {c['status'] for c in checks.values()} == {'pass'}
    assert data['data']['buckets'][0]['bucket'] == '0-10'
    assert data['data']['forecast']['evals'][0]['hours'] is not None
    assert (model / 'doc/eval_readiness/long_answer_load/requests.jsonl').is_file()
    assert len(capsys.readouterr().out.splitlines()) <= 40
    assert gate.main(['--model-dir', str(model), '--stage', 'optimized-vllm',
                      '--probes', 'long_answer_load']) == gate.EXIT_PASS


def test_p4_host_sampling_under_load_is_critical(fake, tmp_path):
    server = fake(host_factor=4.0, tpot_s=0.002)
    model = tmp_path / 'model'
    assert p4.main(p4_args(server, model, tmp_path)) == 2
    _, checks = result(model, 'long_answer_load')
    row = checks['host_sampling_under_load']
    assert row['status'] == 'fail' and row['severity'] == 'critical' and row['value'] > 1.15


def test_p4_failures_leaks_and_recaptures_are_critical(fake, tmp_path):
    server = fake(fail_every=2, leak_running=1, kv_leak=0.2, capture_during_load=True)
    model = tmp_path / 'model'
    assert p4.main(p4_args(server, model, tmp_path, '--no-compare')) == 2
    _, checks = result(model, 'long_answer_load')
    for cid in ('requests_completed', 'slots_released', 'kv_back_to_idle', 'no_new_trace_captures'):
        assert checks[cid]['status'] == 'fail' and checks[cid]['severity'] == 'critical', cid
    assert checks['host_sampling_under_load']['severity'] == 'advisory'


def test_p4_engine_error_in_log_is_critical(fake, tmp_path):
    server = fake(log_line_during_load='ERROR 11-03 [core.py] EngineCore encountered a fatal error\n')
    model = tmp_path / 'model'
    assert p4.main(p4_args(server, model, tmp_path, '--no-compare')) == 2
    _, checks = result(model, 'long_answer_load')
    assert checks['engine_errors']['status'] == 'fail'
    assert 'EngineCore encountered a fatal error' in checks['engine_errors']['detail']


def test_p4_repeated_preemption_critical_only_when_pool_fits(fake, tmp_path):
    server = fake(preempt_per_request=1)
    model = tmp_path / 'fits'
    assert p4.main(p4_args(server, model, tmp_path, '--no-compare')) == 2
    _, checks = result(model, 'long_answer_load')
    assert checks['preemptions']['severity'] == 'critical' and checks['preemptions']['status'] == 'fail'
    small = tmp_path / 'small'
    assert p4.main(p4_args(server, small, tmp_path, '--no-compare', '--kv-pool-tokens', '64')) == 1
    _, checks = result(small, 'long_answer_load')
    assert checks['preemptions']['severity'] == 'advisory' and checks['preemptions']['status'] == 'fail'


def test_p4_slow_but_correct_is_advisory_with_fit_suggestion(fake, tmp_path):
    server = fake(tpot_s=0.004)
    model = tmp_path / 'model'
    args = p4_args(server, model, tmp_path)
    args[args.index('--eval-plan') + 1] = str(plan_file(tmp_path, cap=0.0001, questions=1000))
    args.remove('--functional-floor-tps'), args.remove('1')
    assert p4.main(args) == 1
    data, checks = result(model, 'long_answer_load')
    assert checks['eval_forecast']['status'] == 'fail' and checks['eval_forecast']['severity'] == 'advisory'
    assert checks['functional_floor']['severity'] == 'advisory'  # no floor configured
    row = data['data']['forecast']['evals'][0]
    assert row['over_fit'] and row['fit_questions'] < 1000
    below = tmp_path / 'below'
    assert p4.main(p4_args(server, below, tmp_path, '--no-compare', '--functional-floor-tps', '100000')) == 2
    assert result(below, 'long_answer_load')[1]['functional_floor']['status'] == 'fail'


def test_p4_missing_log_and_uninstrumented_captures_are_advisory(fake, tmp_path):
    server = fake()
    model = tmp_path / 'model'
    args = p4_args(server, model, tmp_path, '--no-compare')
    args[args.index('--server-log') + 1] = str(tmp_path / 'absent.log')
    assert p4.main(args) == 1
    assert result(model, 'long_answer_load')[1]['no_new_trace_captures']['severity'] == 'advisory'
    server.log.write_text('INFO nothing about traces\n')
    other = tmp_path / 'other'
    assert p4.main(p4_args(server, other, tmp_path, '--no-compare')) == 1
    assert 'no trace-capture lines' in result(other, 'long_answer_load')[1]['no_new_trace_captures']['detail']


def test_p4_unreachable_server_is_an_error(tmp_path):
    assert p4.main(['--server-url', 'http://127.0.0.1:9', '--model-dir', str(tmp_path)]) == 3


def test_bucket_rates_and_forecast_math():
    # 2 users, 1 token every 0.1 s -> 10 tok/s per user, 20 aggregate.
    recs = [{'t0': 0.0, 'times': [1.0 + 0.1 * i for i in range(40)], 'usage': {'completion_tokens': 40}}] * 2
    buckets = p4.bucket_rates(recs, 10)
    assert [b['bucket'] for b in buckets] == ['0-10', '10-20', '20-30', '30-40']
    assert buckets[1]['per_user_tps'] == pytest.approx(10.0) and buckets[1]['aggregate_tps'] == pytest.approx(20.0)
    plan = {'ci_cap_hours': {'job': 1.0}, 'fit_fraction': 0.8,
            'evals': [{'name': 'e', 'questions': 3600, 'typical_answer_tokens': 50}]}
    fc = p4.forecast(plan, buckets, 10, 2, 'job')
    row = fc['evals'][0]
    # 50 tokens / 10 tok/s = 5 s per answer per user; 2 in flight -> 3600 * 5 / 2 s = 2.5 h.
    assert row['hours'] == pytest.approx(2.5) and row['extrapolated'] is True
    assert row['over_fit'] and row['fit_questions'] == 1152 and row['fit_output_cap'] is None


def test_profiles_file_and_fallback(tmp_path):
    assert sp.load_profile('default', tmp_path / 'missing.json')['request']['top_k'] == 20
    path = tmp_path / 'profiles.json'
    path.write_text(json.dumps({'profiles': [{'name': 'gpqa', 'source': 's', 'chat': True, 'thinking': True,
                                              'request': {'temperature': 1.0, 'top_k': 40, 'seed': 42}}]}))
    profile = sp.load_profile('gpqa', path)
    url, body = sp.build_request('http://s', 'm', profile, content='q', max_tokens=5)
    assert url.endswith('/v1/chat/completions') and body['seed'] == 42
    assert body['chat_template_kwargs'] == {'enable_thinking': True}
    assert not sp.profile_ignores_eos(profile)
    with pytest.raises(sp.ProbeError):
        sp.load_profile('nope', path)


def test_shared_parsers_match_the_existing_runner(monkeypatch):
    package = ModuleType('readiness_check_stub')
    monkeypatch.setitem(sys.modules, 'requests', SimpleNamespace(get=Mock(), post=Mock()))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=None))
    import importlib.util
    spec = importlib.util.spec_from_file_location('pcc', PLUGIN / 'runtime/readiness_check/prefix_cache_check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = ('# x\nvllm:prefix_cache_queries_total{a="1"} 4\nvllm:prefix_cache_queries_total{a="2"} 6\n'
            'vllm:num_requests_running 3\nbad line\n')
    assert sp.parse_prometheus(text)[0] == module._parse_prometheus_counters(text)
    tree = ast.parse((PLUGIN / 'runtime/readiness_check/run_vllm_server.py').read_text())
    patterns = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                    and getattr(node.targets[0], 'id', '') == '_FATAL_LOG_PATTERNS')
    assert tuple(patterns) == sp.FATAL_LOG_PATTERNS
    del package


# ------------------------------------------------------------------ P5


@pytest.fixture
def book(tmp_path):
    words = ('it was the best of times it was the worst of times it was the age of wisdom '
             'it was the age of foolishness it was the epoch of belief').split()
    sentences = [' '.join(words[(i * 7) % len(words):] + words[:(i * 7) % len(words)]).capitalize() + f' {i}.'
                 for i in range(1500)]
    path = tmp_path / 'book.txt'
    path.write_text(' '.join(sentences))
    return path


def p5_args(server, model_dir, part, book, *extra):
    return ['--part', part, '--server-url', server.url, '--model-dir', str(model_dir), '--server-log', str(server.log),
            '--text-file', str(book), *extra]


def test_p5_accuracy_pass_and_needle_miss(fake, tmp_path, book):
    good = fake()
    model = tmp_path / 'model'
    assert p5.main(p5_args(good, model, 'accuracy', book, '--lengths', '400,1600')) == 0
    data, checks = result(model, 'long_context_accuracy')
    assert checks['recall_400']['status'] == 'pass' and checks['recall_1600']['status'] == 'pass'
    assert data['data']['sample_on_device_mode'] == 'all'
    tokens = [case['prompt_tokens'] for case in data['data']['cases']]
    assert all(abs(t - target) <= 64 for t, target in zip(tokens, [400] * 3 + [1600] * 3)), tokens

    bad = fake(recall_ok=False, sample_mode='none')
    broken = tmp_path / 'broken'
    assert p5.main(p5_args(bad, broken, 'accuracy', book, '--lengths', '400')) == 2
    _, checks = result(broken, 'long_context_accuracy')
    assert checks['recall_400']['severity'] == 'critical' and checks['recall_400']['waivable']
    assert checks['served_on_device_sampling']['status'] == 'fail'
    # HF control rule: the original model misses it too -> the gate downgrades to advisory.
    (broken / 'doc/eval_readiness/hf_recall.txt').write_text('HF also misses the needle at 400\n')
    (broken / 'doc/eval_readiness/limitations.json').write_text(json.dumps({'items': [{
        'probe': 'long_context_accuracy', 'check': 'recall_400', 'reason': 'HF misses it too',
        'evidence': ['doc/eval_readiness/hf_recall.txt'], 'original_model': 'same', 'fix_hours': 0}]}))
    assert gate.main(['--model-dir', str(broken), '--stage', 'vllm', '--probes', 'long_context_accuracy']) == 1


def test_p5_accuracy_clamps_to_served_context(fake, tmp_path, book):
    server = fake(max_model_len=1200)
    model = tmp_path / 'model'
    assert p5.main(p5_args(server, model, 'accuracy', book, '--lengths', '8192,32768', '--depths', '0.5')) == 0
    data, checks = result(model, 'long_context_accuracy')
    # 1200 - 512 answer tokens - 64 template slack - 2% length tolerance.
    assert list(checks) == ['recall_600', 'served_on_device_sampling', 'context_coverage']
    assert checks['context_coverage']['status'] == 'skip'


def test_p5_recall_prompt_never_overshoots_the_target(book):
    text = p5.read_text(book)
    for target in (400, 1600, 5000):
        # A tokenizer with a large fixed overhead: the first estimate overshoots inside the old 2% tolerance.
        _prompt, _answer, tokens = p5.recall_prompt(text, target, 0.5, 1, lambda p: len(p) // 4 + 50)
        assert target - p5.length_tolerance(target) - 64 <= tokens <= target, (target, tokens)


def test_p5_context_length_400_is_a_setup_error_not_a_recall_miss(fake, tmp_path, book):
    server = fake(context_error=True)
    model = tmp_path / 'model'
    assert p5.main(p5_args(server, model, 'accuracy', book, '--lengths', '400', '--depths', '0.5')) == 2
    _, checks = result(model, 'long_context_accuracy')
    assert checks['recall_400']['status'] == 'skip'
    assert checks['probe_setup']['status'] == 'fail' and 'not a recall failure' in checks['probe_setup']['detail']


def test_p5_book_search_falls_back_to_the_older_path(tmp_path):
    old = tmp_path / 'tt-metal/models/tt_transformers/tests/tale-of-two-cities.txt.bz2'
    old.parent.mkdir(parents=True)
    import bz2
    old.write_bytes(bz2.compress(b'It was the best of times.'))
    assert p5.find_book(None, tmp_path / 'tt-metal') == old
    new = tmp_path / 'tt-metal/models/ttt_compat/tests/tale-of-two-cities.txt.bz2'
    new.parent.mkdir(parents=True)
    new.write_bytes(old.read_bytes())
    assert p5.find_book(None, tmp_path / 'tt-metal') == new
    assert p5.read_text(new) == 'It was the best of times.'


def write_chunk(model, mnbt, itl, ttft=1000.0):
    path = model / 'doc/eval_readiness/long_context_timing' / f'chunk_{mnbt}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'max_num_batched_tokens': mnbt, 'worst_itl_ms': itl,
                                'ttft_ms': {'4096': ttft}, 'errors': []}))


def test_p5_timing_clean_server_passes(fake, tmp_path, book):
    server = fake(mnbt=2048)
    model = tmp_path / 'model'
    for mnbt, itl in ((512, 200.0), (2048, 600.0), (4096, 1800.0)):
        write_chunk(model, mnbt, itl)
    assert p5.main(p5_args(server, model, 'timing', book, '--lengths', '100,200,400', '--ttft-reps', '2')) == 0
    data, checks = result(model, 'long_context_timing')
    assert data['data']['chunk_pick'] == 2048 and data['data']['served_max_num_batched_tokens'] == 2048
    assert all(row['tpot_ms'] for row in data['data']['tpot'])


def test_p5_timing_flags_cliff_unaligned_ttft_and_missing_sweep(fake, tmp_path, book):
    server = fake(cliff_at=400, misaligned_slow=True, mnbt=512)
    model = tmp_path / 'model'
    write_chunk(model, 512, 200.0)
    write_chunk(model, 2048, 600.0)
    assert p5.main(p5_args(server, model, 'timing', book, '--lengths', '100,200,400', '--ttft-reps', '2')) == 1
    _, checks = result(model, 'long_context_timing')
    for cid in ('tpot_cliff', 'ttft_127_vs_128', 'chunk_size_sweep'):
        assert checks[cid]['status'] == 'fail' and checks[cid]['severity'] == 'advisory', cid
    assert '200->400' in checks['tpot_cliff']['detail']
    write_chunk(model, 4096, 5000.0)
    assert p5.main(p5_args(server, model, 'timing', book, '--lengths', '100,200', '--ttft-reps', '1')) == 1
    _, checks = result(model, 'long_context_timing')
    assert 'largest value within the ITL bound is 2048; server runs 512' in checks['chunk_size_sweep']['detail']


def test_p5_chunk_part_records_ttft_and_worst_itl(fake, tmp_path, book):
    server = fake(mnbt=1024)
    model = tmp_path / 'model'
    args = p5_args(server, model, 'chunk', book, '--chunk-prompts', '256,512', '--decode-users', '3',
                   '--decode-warmup-s', '0.05', '--decoder-max-tokens', '100000')
    assert p5.main(args) == 0
    out = json.loads((model / 'doc/eval_readiness/long_context_timing/chunk_1024.json').read_text())
    assert out['max_num_batched_tokens'] == 1024 and set(out['ttft_ms']) == {'256', '512'}
    assert out['worst_itl_ms'] is not None and not out['errors']
    assert server.active == 0 or _drains(server)


def _drains(server):
    return sp.wait_until(lambda: server.active == 0, 5, 0.05)


# ------------------------------------------------------------------ stage 10 gate wiring


def stage10_evidence(model: Path) -> None:
    doc = model / 'doc'
    (doc / 'autoregressive').mkdir(parents=True)
    (doc / 'full_model').mkdir()
    (model / 'readiness_vllm').mkdir()
    completion = ' '.join(f'word{i}' for i in range(60))
    (doc / 'autoregressive/autoregressive_meta.json').write_text(json.dumps({'tt': {'token_ids': list(range(60))}}))
    (doc / 'autoregressive/tt_completion.txt').write_text(completion)
    (model / 'readiness_vllm/vllm_qualitative_outputs.json').write_text(json.dumps(
        [{'prompt': 'p', 'greedy_completion': completion, 'sampled_completion': completion}]))
    (doc / 'context_contract.json').write_text(json.dumps({'hf_advertised_context': 8192, 'current_supported_context': 8192}))
    (doc / 'full_model/serving_contract.json').write_text(json.dumps({
        'plugin': {'repository': 'tenstorrent/vllm-tt-plugin', 'commit': 'a' * 40}, 'vllm': {'version': 'v0'},
        'documents_read': ['docs/MODEL_INTERFACE.md'],
        'rows': [{'item': 'supports_prefix_caching', 'kind': 'capability', 'status': 'not_supported',
                  'declaration': 'documented default', 'reason': 'recurrent_state: not recoverable from KV blocks'}]}))
    # A burst ratio that misses the old Stage 10 gate (0.8 > 0.6): advisory after D5.
    cold = {'reset': True, 'queries': 4096, 'hits': 0, 'hit_rate': 0.0}
    (model / 'readiness_vllm/vllm_prefill_burst_benchmark.json').write_text(json.dumps(
        {'completed_requests': 32, 'config': {'num_requests': 32}, 'ttft_ms': {'p99': 1000.0}, 'prefix_cache': cold,
         'serial_reference': {'ttft_ratio_p99': 0.8, 'ttft_ratio_mean': 0.8, 'config_matches': True,
                              'serial_prefix_cache': cold}}))


def test_stage10_check_script_merges_prefill_and_eval_readiness(tmp_path):
    import os
    import subprocess
    from readiness_check.eval_readiness_result import check, write_result
    model = tmp_path / 'models/autoports/org_model'
    stage10_evidence(model)
    env = dict(os.environ, MODEL_DIR=str(model), PATH=f'{Path(sys.executable).parent}:{os.environ["PATH"]}')
    script = PLUGIN / 'prompts/model_bringup_multigoal/10-optimized-vllm.check.sh'

    def gate_run():
        return subprocess.run(['bash', str(script)], cwd=tmp_path, env=env, text=True, capture_output=True)

    missing = gate_run()
    assert missing.returncode == 2, missing.stdout + missing.stderr
    assert 'long_answer_load: no result' in missing.stdout
    write_result(model, 'long_answer_load', [check('requests_completed', True)], command=['x'])
    write_result(model, 'long_context_timing', [check('tpot_cliff', True, severity='advisory')], command=['x'])
    advisory = gate_run()
    assert advisory.returncode == 1, advisory.stdout + advisory.stderr
    assert 'advisory: prefill burst p99 TTFT ratio' in advisory.stderr
    write_result(model, 'long_answer_load', [check('host_sampling_under_load', False, detail='2x')], command=['x'])
    assert gate_run().returncode == 2
