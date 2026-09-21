"""Freeze content-identified samples before looking at any model scores."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

SEED = 'tt-bringup-benchmark-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def indices(task_name, docs, count):
    if count < 1 or count > len(docs):
        raise ValueError(f'{task_name}: invalid sample size {count}/{len(docs)}')
    ranked = sorted(range(len(docs)), key=lambda i: digest([SEED, task_name, docs[i]]))
    return sorted(ranked[:count])


def allocate(sizes, total):
    """Proportional subject allocation, with deterministic largest remainders."""
    population = sum(sizes.values())
    if not len(sizes) <= total <= population:
        raise ValueError('sample budget must cover each subject and fit the dataset')
    quota = {k: total * n / population for k, n in sizes.items()}
    result = {k: max(1, math.floor(q)) for k, q in quota.items()}
    while sum(result.values()) > total:
        k = max((k for k in sizes if result[k] > 1), key=lambda k: (result[k] - quota[k], k))
        result[k] -= 1
    while sum(result.values()) < total:
        k = max((k for k in sizes if result[k] < sizes[k]), key=lambda k: (quota[k] - result[k], k))
        result[k] += 1
    return result


def flatten(tasks):
    result = {}
    for key, value in tasks.items():
        if isinstance(value, dict):
            result.update(flatten(value))
        else:
            result[key] = value
    return result


def reuse_indices(source, population_hash, count):
    """Keep the same questions when an upstream task changes only its recipe."""
    candidates = [(name, task) for name, task in source['tasks'].items()
                  if task['population_sha256'] == population_hash]
    if len(candidates) != 1:
        raise ValueError('recipe variant requires one identical source evaluation population')
    name, task = candidates[0]
    if len(task['indices']) != count:
        raise ValueError('recipe variant must preserve the frozen sample count')
    return list(task['indices']), name


def prepare(task_groups, budgets, output, reuse_manifest=None):
    from importlib.metadata import version
    from lm_eval.tasks import get_task_dict

    manifest = {'schema_version': 1, 'seed': SEED, 'harness_version': version('lm_eval'), 'groups': {}, 'tasks': {}}
    source = None
    if reuse_manifest:
        source = json.loads(Path(reuse_manifest).read_text())
        if digest({k: v for k, v in source.items() if k != 'manifest_sha256'}) != source['manifest_sha256']:
            raise ValueError('source subset manifest checksum mismatch')
        if source['harness_version'] != manifest['harness_version']:
            raise ValueError('source subset harness version differs')
        manifest['reused_manifest_sha256'] = source['manifest_sha256']
    samples = {}
    for group in task_groups:
        tasks = flatten(get_task_dict([group]))
        documents = {k: list(v.eval_docs) for k, v in tasks.items()}
        counts = allocate({k: len(v) for k, v in documents.items()}, budgets[group])
        manifest['groups'][group] = {'sample_count': sum(counts.values()), 'population': sum(map(len, documents.values())), 'tasks': sorted(tasks)}
        for name, docs in documents.items():
            population_hash = digest(docs)
            selected, source_task = reuse_indices(source, population_hash, counts[name]) if source else (indices(name, docs, counts[name]), None)
            samples[name] = selected
            manifest['tasks'][name] = {
                'population': len(docs), 'indices': selected,
                'document_sha256': [digest(docs[i]) for i in selected],
                'population_sha256': population_hash,
                'num_fewshot': tasks[name].config.num_fewshot or 0,
                'fewshot_sha256': digest(list(tasks[name].fewshot_docs())) if tasks[name].config.num_fewshot else None,
                'dataset_fingerprint': getattr(tasks[name].eval_docs, '_fingerprint', None),
            }
            if source_task:
                manifest['tasks'][name]['reused_from_task'] = source_task
    manifest['manifest_sha256'] = digest(manifest)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [('manifest.json', manifest), ('samples.json', samples)]:
        target = output / name
        if target.exists() and json.loads(target.read_text()) != data:
            raise ValueError(f'refusing to replace a different frozen subset: {target}')
        target.write_text(json.dumps(data, indent=2) + '\n')
    return manifest
