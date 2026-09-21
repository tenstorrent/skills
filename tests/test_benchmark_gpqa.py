"""GPQA choices remain identical through fresh task loads and transformed caches."""
import json
from pathlib import Path
import random
import sys
import types

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup/runtime'
sys.path.insert(0, str(RUNTIME))
from benchmark_stage import gpqa
from benchmark_stage.subsets import digest, prepare


@pytest.fixture
def upstream_preprocess(monkeypatch):
    module = types.ModuleType('lm_eval.tasks.gpqa.cot_zeroshot.utils')
    module.preprocess = lambda text: text.strip()
    monkeypatch.setitem(sys.modules, module.__name__, module)


def source_rows():
    return [dict(Question=f'Question {i}', **{
        'Incorrect Answer 1': ' wrong one ', 'Incorrect Answer 2': 'wrong two',
        'Incorrect Answer 3': 'wrong three', 'Correct Answer': ' correct ',
    }) for i in range(12)]


class MemoryDataset(list):
    _fingerprint = 'synthetic-input'

    def map(self, function, *, load_from_cache_file=True):
        assert load_from_cache_file is False
        return MemoryDataset([{**row, **function(row)} for row in self])


def test_gpqa_matches_upstream_seed_zero_without_global_rng_dependency(upstream_preprocess):
    data = MemoryDataset(source_rows())
    random.seed(814)
    state = random.getstate()
    first = gpqa.process_docs(data)
    assert random.getstate() == state
    random.seed(196)
    assert gpqa.process_docs(data) == first
    expected_rng = random.Random(0)
    for source, row in zip(data, first):
        choices = [source[k].strip() for k in (
            'Incorrect Answer 1', 'Incorrect Answer 2', 'Incorrect Answer 3', 'Correct Answer')]
        expected_rng.shuffle(choices)
        assert row['choices'] == choices
        assert row['answer'] == f'({chr(65 + choices.index("correct"))})'
        assert row['Question'] == source['Question']


def test_gpqa_ignores_poisoned_map_cache_and_matches_memory(tmp_path, upstream_preprocess):
    datasets = pytest.importorskip('datasets')
    from datasets.arrow_writer import ArrowWriter
    source = datasets.Dataset.from_list(source_rows())
    source.save_to_disk(str(tmp_path / 'source'))
    disk = datasets.load_from_disk(str(tmp_path / 'source'))
    first = gpqa.process_docs(disk)
    expected = list(first)
    cached_path = Path(first.cache_files[0]['filename'])
    poisoned = [{**row, 'answer': '(Z)'} for row in expected]
    replacement = tmp_path / 'poisoned.arrow'
    with ArrowWriter(path=str(replacement)) as writer:
        for row in poisoned:
            writer.write(row)
        writer.finalize()
    replacement.replace(cached_path)
    assert list(datasets.Dataset.from_file(str(cached_path))) == poisoned
    random.seed(918)
    assert list(gpqa.process_docs(disk)) == expected
    assert list(gpqa.process_docs(source)) == expected
    caching = datasets.is_caching_enabled()
    try:
        datasets.disable_caching()
        random.seed(819)
        assert list(gpqa.process_docs(disk)) == expected
        assert list(gpqa.process_docs(source)) == expected
    finally:
        if caching:
            datasets.enable_caching()


@pytest.fixture
def fake_harness(monkeypatch, tmp_path, upstream_preprocess):
    """Exercise wrapper load boundaries without downloads or a serving backend."""
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '0.4.13')
    root = types.ModuleType('lm_eval')
    tasks = types.ModuleType('lm_eval.tasks')
    tasks.__file__ = str(tmp_path / 'tasks/__init__.py')
    root.tasks = tasks
    loader = types.ModuleType('lm_eval.tasks._yaml_loader')
    original_config = dict(task=gpqa.TASK, dataset_name='gpqa_diamond', num_fewshot=0,
                           doc_to_text='upstream prompt', doc_to_target='answer',
                           generation_kwargs={'temperature': 0}, process_docs='upstream')
    loads = []

    def load_yaml(path, **kwargs):
        assert Path(path).name == gpqa.TASK + '.yaml'
        assert kwargs == {'resolve_func': True, 'recursive': True}
        return dict(original_config)

    loader.load_yaml = load_yaml

    class Task:
        def __init__(self, config):
            self.config = types.SimpleNamespace(**config)
            # Construction must already see the deterministic override.
            self.initial_docs = self.config.process_docs(MemoryDataset(source_rows()))

        @property
        def eval_docs(self):
            return self.config.process_docs(MemoryDataset(source_rows()))

    def get_task_dict(specs):
        config = specs[0]
        assert config['process_docs'] is gpqa.process_docs
        assert {k: v for k, v in config.items() if k != 'process_docs'} == {
            k: v for k, v in original_config.items() if k != 'process_docs'}
        task = Task(config)
        loads.append(list(task.initial_docs))
        return {gpqa.TASK: task}

    tasks.get_task_dict = get_task_dict
    models = types.ModuleType('lm_eval.models.openai_completions')

    class LocalChatCompletion:
        def __init__(self, **kwargs):
            pass

        def parse_generations(self, outputs, **kwargs):
            return [r['choices'][0]['message']['content'] for r in outputs]

    models.LocalChatCompletion = LocalChatCompletion
    evaluator = types.ModuleType('lm_eval.evaluator')

    def simple_evaluate(*, model, tasks, samples, **kwargs):
        random.seed(0)
        task = get_task_dict(tasks)[gpqa.TASK]
        docs = list(task.eval_docs)
        selected = samples[gpqa.TASK]
        model.parse_generations([dict(choices=[dict(
            message=dict(content='The answer is (A).'), finish_reason='stop')]) for _ in selected])
        return dict(results={gpqa.TASK: {'exact_match,strict-match': 0.5}},
                    samples={gpqa.TASK: [dict(doc_id=i, doc=docs[i]) for i in selected]})

    evaluator.simple_evaluate = simple_evaluate
    root.evaluator = evaluator
    for module in (root, tasks, loader, models, evaluator):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return loads


def test_prepare_preflight_and_evaluator_share_gpqa_policy(tmp_path, fake_harness):
    from benchmark_stage.evaluate import evaluate
    random.seed(198)
    manifest = prepare([gpqa.TASK], {gpqa.TASK: 6}, tmp_path / 'subset')
    assert manifest['tasks'][gpqa.TASK]['document_processing'] == gpqa.PROCESSING_POLICY
    random.seed(819)
    result = evaluate(model='org/model', base_url='http://unused',
                      manifest_path=tmp_path / 'subset/manifest.json', group=gpqa.TASK,
                      output=tmp_path / 'evaluation')
    assert len(fake_harness) == 3  # prepare, preflight and evaluator's separate load
    assert fake_harness[0] == fake_harness[1] == fake_harness[2]
    assert result['benchmark_stage']['document_processing'] == gpqa.PROCESSING_POLICY
    assert result['benchmark_stage']['responses'] == 6
    rows = [json.loads(line) for line in
            (tmp_path / f'evaluation/samples_{gpqa.TASK}.jsonl').read_text().splitlines()]
    frozen = manifest['tasks'][gpqa.TASK]
    assert [digest(row['doc']) for row in rows] == frozen['document_sha256']


@pytest.mark.parametrize('policy', [None, dict(gpqa.PROCESSING_POLICY, seed=9)])
def test_gpqa_rejects_missing_or_changed_processing_policy(tmp_path, fake_harness, policy):
    from benchmark_stage.evaluate import evaluate
    manifest = prepare([gpqa.TASK], {gpqa.TASK: 6}, tmp_path / 'subset')
    manifest['tasks'][gpqa.TASK]['document_processing'] = policy
    manifest['manifest_sha256'] = digest({k: v for k, v in manifest.items() if k != 'manifest_sha256'})
    path = tmp_path / 'subset/manifest.json'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='document-processing policy'):
        evaluate(model='org/model', base_url='http://unused', manifest_path=path,
                 group=gpqa.TASK, output=tmp_path / 'evaluation')


def test_other_tasks_keep_upstream_loading_and_policy():
    for name in ('mmlu_pro', 'gsm8k_cot', 'ifeval', 'mmlu_pro_llama', 'gsm8k_cot_llama'):
        assert gpqa.task_spec(name) == name
        assert gpqa.processing_policy(name) is None
