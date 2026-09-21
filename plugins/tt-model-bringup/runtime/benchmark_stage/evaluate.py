"""Run upstream lm-eval tasks without replacing their prompts or scorers."""
from __future__ import annotations

import json
import time
from pathlib import Path

from benchmark_stage.subsets import digest, flatten


def evaluate(*, model, base_url, manifest_path, group, output, generation=None):
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
    tasks = flatten(get_task_dict([group]))
    if sorted(tasks) != manifest['groups'][group]['tasks']:
        raise ValueError('task membership differs from frozen subset')
    samples = {}
    for name, task in tasks.items():
        docs = list(task.eval_docs)
        frozen = manifest['tasks'][name]
        if len(docs) != frozen['population'] or digest(docs) != frozen['population_sha256']:
            raise ValueError(f'{name}: dataset changed since subset freeze')
        if 'num_fewshot' in frozen:
            shots = task.config.num_fewshot or 0
            if shots != frozen['num_fewshot'] or (shots and digest(list(task.fewshot_docs())) != frozen['fewshot_sha256']):
                raise ValueError(f'{name}: few-shot examples changed since subset freeze')
        samples[name] = frozen['indices']

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    response_file = output / 'responses.jsonl'

    class RecordedChat(LocalChatCompletion):
        def parse_generations(self, outputs, **kwargs):
            rows = outputs if isinstance(outputs, list) else [outputs]
            with response_file.open('a') as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
            for row in rows:
                if not row.get('choices') or any(not (c.get('message', {}).get('content') or '').strip() for c in row['choices']):
                    raise ValueError('missing final answer in API response; inspect reasoning/parser configuration')
            return super().parse_generations(outputs, **kwargs)

    backend = RecordedChat(model=model, base_url=base_url.rstrip('/') + '/v1/chat/completions',
                           num_concurrent=32, max_retries=0, timeout=900,
                           tokenized_requests=False, tokenizer_backend=None, max_gen_toks=2048)
    started = time.time()
    result = evaluator.simple_evaluate(
        model=backend, tasks=[group], samples=samples,
        apply_chat_template=True, fewshot_as_multiturn=True, log_samples=True,
        gen_kwargs=generation or None, random_seed=0, numpy_random_seed=1234,
        torch_random_seed=1234, fewshot_random_seed=1234,
    )
    elapsed = time.time() - started
    if result is None:
        raise RuntimeError('lm-eval returned no results')
    sample_results = result.pop('samples', {})
    for task_name, rows in sample_results.items():
        with (output / f'samples_{task_name}.jsonl').open('w') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    responses = [json.loads(line) for line in response_file.read_text().splitlines()]
    reasons = {}
    for response in responses:
        for choice in response['choices']:
            reason = choice.get('finish_reason', 'missing')
            reasons[reason] = reasons.get(reason, 0) + 1
    expected = manifest['groups'][group]['sample_count']
    result['benchmark_stage'] = {
        'model': model, 'group': group, 'subset_sha256': expected_hash,
        'concurrency': 32, 'elapsed_seconds': elapsed,
        'expected_samples': expected, 'responses': len(responses),
        'finish_reasons': reasons, 'generation_overrides': generation or {},
        'template': 'native server chat template; structured messages exactly once',
        'fewshot_as_multiturn': True,
    }
    (output / 'results.json').write_text(json.dumps(result, indent=2, default=str) + '\n')
    if len(responses) != expected:
        raise RuntimeError(f'incomplete requests: {len(responses)}/{expected}')
    return result
