"""Run upstream lm-eval tasks without replacing their prompts or scorers."""
from __future__ import annotations

from contextvars import ContextVar
import json
import time
from pathlib import Path

from benchmark_stage.gpqa import processing_policy, task_spec
from benchmark_stage.responses import read_jsonl, scoring_response
from benchmark_stage.subsets import digest, flatten


def evaluate(*, model, base_url, manifest_path, group, output, generation=None):
    return evaluate_groups(model=model, base_url=base_url, manifest_path=manifest_path,
                           groups=[group], output=output, generation=generation, shared=False)[group]


def evaluate_groups(*, model, base_url, manifest_path, groups, output, generation=None, shared=True):
    from importlib.metadata import version
    from lm_eval import evaluator
    from lm_eval.models.openai_completions import LocalChatCompletion
    from lm_eval.tasks import get_task_dict

    manifest = json.loads(Path(manifest_path).read_text())
    if version('lm_eval') != manifest['harness_version']:
        raise ValueError('harness version differs from frozen subset')
    expected_hash = manifest['manifest_sha256']
    if digest({k: v for k, v in manifest.items() if k != 'manifest_sha256'}) != expected_hash:
        raise ValueError('subset manifest checksum mismatch')
    if not groups or len(groups) != len(set(groups)):
        raise ValueError('evaluation groups must be nonempty and unique')
    tasks, membership = {}, {}
    for group in groups:
        loaded = flatten(get_task_dict([task_spec(group)]))
        if sorted(loaded) != manifest['groups'][group]['tasks']:
            raise ValueError('task membership differs from frozen subset')
        if set(loaded) & set(tasks):
            raise ValueError('evaluation groups must have disjoint child tasks')
        tasks.update(loaded)
        membership.update({name: group for name in loaded})
    samples = {}
    for name, task in tasks.items():
        docs = list(task.eval_docs)
        frozen = manifest['tasks'][name]
        if frozen.get('document_processing') != processing_policy(name):
            raise ValueError(f'{name}: document-processing policy differs from frozen subset')
        if len(docs) != frozen['population'] or digest(docs) != frozen['population_sha256']:
            raise ValueError(f'{name}: dataset changed since subset freeze')
        if 'num_fewshot' in frozen:
            shots = task.config.num_fewshot or 0
            if shots != frozen['num_fewshot'] or (shots and digest(list(task.fewshot_docs())) != frozen['fewshot_sha256']):
                raise ValueError(f'{name}: few-shot examples changed since subset freeze')
        samples[name] = frozen['indices']

    output = Path(output)
    paths = {group: output / group if shared else output for group in groups}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=False)
    active_request = ContextVar('benchmark_request', default=None)

    class RecordedChat(LocalChatCompletion):
        def generate_until(self, requests, **kwargs):
            if shared:
                # Upstream processes different generation-kwargs groups serially.
                # Require one exact group before sending any request; nested dicts
                # must match by value, not merely by the Collator's grouping hash.
                if len({digest(req.args[1]) for req in requests}) != 1:
                    raise ValueError('shared accuracy requires identical effective generation kwargs')
                self.request_metadata = {}
                for req in requests:
                    group = membership[req.task_name]
                    key = digest([req.args[0], req.args[1]])
                    if key in self.request_metadata or req.repeats != 1:
                        raise ValueError('shared accuracy requires unique API requests and one repeat')
                    self.request_metadata[key] = {
                        'request_sha256': key, 'group': group, 'task': req.task_name,
                        'doc_id': samples[req.task_name][req.doc_id],
                    }
            return super().generate_until(requests, **kwargs)

        async def amodel_call(self, *args, cache_keys=None, **kwargs):
            token = None
            if shared:
                if not cache_keys or len(cache_keys) != 1:
                    raise ValueError('shared accuracy requires one chat request per API call')
                key = digest(cache_keys[0])
                token = active_request.set(self.request_metadata[key])
            try:
                return await super().amodel_call(*args, cache_keys=cache_keys, **kwargs)
            finally:
                if token is not None:
                    active_request.reset(token)

        def parse_generations(self, outputs, **kwargs):
            rows = outputs if isinstance(outputs, list) else [outputs]
            if shared:
                metadata = active_request.get()
                group = metadata['group']
            else:
                group = groups[0]
            with (paths[group] / 'responses.jsonl').open('a') as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
            if shared:
                with (paths[group] / 'request_links.jsonl').open('a') as f:
                    for row in rows:
                        response_id = row.get('id') if isinstance(row, dict) else None
                        f.write(json.dumps({**metadata, 'response_id': response_id}) + '\n')
            normalized = [scoring_response(row)[0] for row in rows]
            return super().parse_generations(normalized if isinstance(outputs, list) else normalized[0], **kwargs)

    # Long reasoning answers can exceed 15 minutes. The parent runner enforces
    # the complete stage's deadline, including queued and in-flight requests.
    request_timeout_seconds = 3600
    backend = RecordedChat(model=model, base_url=base_url.rstrip('/') + '/v1/chat/completions',
                           num_concurrent=32, max_retries=0, timeout=request_timeout_seconds,
                           tokenized_requests=False, tokenizer_backend=None, max_gen_toks=2048)
    started = time.time()
    result = evaluator.simple_evaluate(
        model=backend, tasks=[task_spec(group) for group in groups], samples=samples,
        apply_chat_template=True, fewshot_as_multiturn=True, log_samples=True,
        gen_kwargs=generation or None, random_seed=0, numpy_random_seed=1234,
        torch_random_seed=1234, fewshot_random_seed=1234,
    )
    elapsed = time.time() - started
    if result is None:
        raise RuntimeError('lm-eval returned no results')
    sample_results = result.pop('samples', {})
    outputs = {}
    for group in groups:
        path = paths[group]
        children = manifest['groups'][group]['tasks']
        for task_name in children:
            with (path / f'samples_{task_name}.jsonl').open('w') as f:
                for row in sample_results[task_name]:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        responses = read_jsonl(path / 'responses.jsonl')
        reasons = {}
        for response in responses:
            for choice in response['choices']:
                reason = choice.get('finish_reason', 'missing')
                reasons[reason] = reasons.get(reason, 0) + 1
        expected = manifest['groups'][group]['sample_count']
        projected = dict(result)
        for field in ('results', 'groups', 'group_subtasks', 'configs', 'versions', 'n-shot', 'higher_is_better', 'n-samples'):
            if isinstance(projected.get(field), dict):
                projected[field] = {k: v for k, v in projected[field].items() if k in {*children, group}}
        projected['benchmark_stage'] = {
            'model': model, 'group': group, 'subset_sha256': expected_hash,
            'concurrency': 32, 'elapsed_seconds': elapsed,
            'timing_scope': 'shared accuracy pass' if shared else 'single task group',
            'shared_groups': groups if shared else [],
            'request_timeout_seconds': request_timeout_seconds,
            'expected_samples': expected, 'responses': len(responses),
            'finish_reasons': reasons, 'generation_overrides': generation or {},
            'empty_final_length_responses': sum(scoring_response(row)[1] for row in responses),
            'template': 'native server chat template; structured messages exactly once',
            'fewshot_as_multiturn': True,
        }
        if policy := processing_policy(group):
            projected['benchmark_stage']['document_processing'] = policy
        (path / 'results.json').write_text(json.dumps(projected, indent=2, default=str) + '\n')
        if len(responses) != expected:
            raise RuntimeError(f'incomplete requests for {group}: {len(responses)}/{expected}')
        outputs[group] = projected
    return outputs
