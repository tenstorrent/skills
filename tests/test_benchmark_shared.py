"""Exercise the pinned upstream API scheduler against a local fake HTTP endpoint."""
import copy
import json
from pathlib import Path
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/runtime'))
from benchmark_stage.evaluate import evaluate_groups
from benchmark_stage.subsets import digest
from benchmark_stage.responses import read_jsonl

ANSWER = "identical answer\u2028same record\u2029same paragraph\u0085same value"


@pytest.mark.parametrize('mutation', [None, 'nested_kwargs', 'duplicate', 'repeats', 'malformed_response'])
def test_shared_pool_preserves_request_identity_and_global_concurrency(tmp_path, monkeypatch, mutation):
    pytest.importorskip('lm_eval')
    from importlib.metadata import version
    from lm_eval import evaluator, tasks
    from lm_eval.api.instance import Instance
    from lm_eval.models.api_models import JsonChatStr
    assert version('lm_eval') == '0.4.13'
    selected = list(range(1, 73, 2))
    documents = {name: [{'prompt': f'{name}:{i:03}', 'note': 'GPQA\u2028question'} for i in range(80)] for name in ('alpha', 'beta')}
    manifest = {'harness_version': '0.4.13', 'groups': {}, 'tasks': {}}
    for name, docs in documents.items():
        manifest['groups'][name] = {'tasks': [name], 'sample_count': len(selected), 'population': len(docs)}
        manifest['tasks'][name] = {'indices': selected, 'population': len(docs), 'population_sha256': digest(docs),
                                  'document_sha256': [digest(docs[i]) for i in selected]}
    manifest['manifest_sha256'] = digest(manifest)
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(tasks, 'get_task_dict', lambda specs: {
        name: types.SimpleNamespace(eval_docs=documents[name]) for name in specs})
    state = {'active': 0, 'peak': 0, 'seen': [], 'first_done': None}
    lock = threading.Lock()
    filled = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert 'do_sample' not in payload
            label = payload['messages'][0]['content']
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['seen'].append((label, time.monotonic()))
                if state['active'] == 32: filled.set()
            assert filled.wait(3), 'upstream did not fill the shared pool'
            slow = label == 'alpha:001'
            time.sleep(0.7 if slow else 0.02)
            response = {'id': label, 'choices': [{'index': 0, 'message': {
                'content': None if slow else ANSWER,
                'reasoning': 'Never score this as a final answer'}, 'finish_reason': 'length' if slow else 'stop'}],
                'usage': {'completion_tokens': 32 if slow else 1}}
            if mutation == 'malformed_response' and slow:
                response = 'malformed API payload'
            data = json.dumps(response).encode()
            # Finished server work before exposing the reply to the next client request.
            with lock:
                state['active'] -= 1
                if slow: state['first_done'] = time.monotonic()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def fake_evaluate(*, model, samples, gen_kwargs, **kwargs):
        requests = []
        for name, docs in documents.items():
            for position, doc_id in enumerate(samples[name]):
                prompt = JsonChatStr(json.dumps([{'role': 'user', 'content': docs[doc_id]['prompt']}]))
                requests.append(Instance(request_type='generate_until', doc=docs[doc_id],
                    arguments=(prompt, copy.deepcopy(gen_kwargs)), idx=0, metadata=(name, position, 1)))
        if mutation == 'nested_kwargs': requests[-1].args[1]['chat_template_kwargs']['enable_thinking'] = False
        elif mutation == 'duplicate': requests[-1].arguments = requests[0].arguments
        elif mutation == 'repeats': requests[-1].repeats = 2
        answers = model.generate_until(requests)
        assert len(answers) == 72
        scored = {name: [] for name in documents}
        for request, answer in zip(requests, answers):
            doc_id = samples[request.task_name][request.doc_id]
            scored[request.task_name].append(dict(doc_id=doc_id, doc=request.doc, arguments=[request.args],
                resps=[[answer]], filter='none', exact_match=float(answer == ANSWER)))
        return {'results': {name: {'exact_match,none': sum(x['exact_match'] for x in rows) / len(rows)}
                            for name, rows in scored.items()}, 'samples': scored}
    monkeypatch.setattr(evaluator, 'simple_evaluate', fake_evaluate)
    try:
        kwargs = dict(model='test/model', base_url=f'http://127.0.0.1:{server.server_port}',
            manifest_path=manifest_path, groups=['alpha', 'beta'], output=tmp_path / 'run',
            generation={'max_gen_toks': 32, 'do_sample': True, 'until': [],
                        'chat_template_kwargs': {'enable_thinking': True}})
        if mutation == 'malformed_response':
            with pytest.raises(ValueError, match='invalid API response'):
                evaluate_groups(**kwargs)
            assert 'malformed API payload' in read_jsonl(tmp_path / 'run/alpha/responses.jsonl')
            return
        if mutation:
            with pytest.raises(ValueError): evaluate_groups(**kwargs)
            assert not state['seen']
            return
        result = evaluate_groups(**kwargs)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert state['peak'] == 32
    assert min(t for label, t in state['seen'] if label.startswith('beta:')) < state['first_done']
    assert result['alpha']['results']['alpha']['exact_match,none'] == 35 / 36
    assert result['beta']['results']['beta']['exact_match,none'] == 1
    for name in documents:
        root = tmp_path / 'run' / name
        read = lambda filename: read_jsonl(root / filename)
        links, raw, scored = read('request_links.jsonl'), read('responses.jsonl'), read(f'samples_{name}.jsonl')
        assert len(raw) == len(links) == len(scored) == 36
        assert sorted(x['doc_id'] for x in links) == selected
        assert sorted(x['doc_id'] for x in scored) == selected
        hashes = {row['doc_id']: digest(row['arguments'][0]) for row in scored}
        for link, response in zip(links, raw):
            assert link['response_id'] == response['id'] == f'{name}:{link["doc_id"]:03}'
            assert link['request_sha256'] == hashes[link['doc_id']]
        metadata = result[name]['benchmark_stage']
        assert metadata['responses'] == metadata['expected_samples'] == 36
        assert metadata['empty_final_length_responses'] == int(name == 'alpha')
        assert metadata['timing_scope'] == 'shared accuracy pass'
    assert any(x['choices'][0]['message']['content'] is None for x in read('responses.jsonl')) is False
    alpha_raw = read_jsonl(tmp_path / 'run/alpha/responses.jsonl')
    assert sum(x['choices'][0]['message']['content'] is None for x in alpha_raw) == 1
