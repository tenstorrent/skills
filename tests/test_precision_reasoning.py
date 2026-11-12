"""P1 precision-on-reasoning probe: pure math and the orchestration on a tiny CPU fake generator."""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "tt-model-bringup"
sys.path.insert(0, str(PLUGIN / "runtime"))

from readiness_check import run_precision_reasoning as p1  # noqa: E402

spec = importlib.util.spec_from_file_location("check_eval_readiness_p1", PLUGIN / "scripts" / "check_eval_readiness.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

VOCAB = 48


class TinyLM:
    """Deterministic bigram-context model; ``quant`` rounds weights to emulate a coarse datatype."""

    def __init__(self, noise=0.0, quant=0.0):
        g = torch.Generator().manual_seed(7)
        self.E = torch.randn(VOCAB, 16, generator=g)
        self.F = torch.randn(VOCAB, 16, generator=g)
        self.W1 = torch.randn(16, 32, generator=g) / 4
        self.W2 = torch.randn(32, VOCAB, generator=g)
        if noise:
            gn = torch.Generator().manual_seed(11)
            self.W2 = self.W2 + noise * torch.randn(self.W2.shape, generator=gn)
        if quant:
            self.W1 = torch.round(self.W1 / quant) * quant
            self.W2 = torch.round(self.W2 / quant) * quant

    def logits(self, ids):
        x = torch.tensor(ids)
        prev = torch.cat([x[:1], x[:-1]])
        return torch.tanh((self.E[x] + self.F[prev]) @ self.W1) @ self.W2


class FakeGenerator:
    def __init__(self, lm):
        self.lm = lm
        self.torn_down = False

    def prefill_logits(self, ids):
        return self.lm.logits(ids)[None]

    def generate(self, prompt_token_ids, max_new_tokens, *, next_input=None, enable_trace=True,
                 temperature=1.0, top_p=1.0, seed=0):
        assert enable_trace is True
        g = torch.Generator().manual_seed(seed)
        ids, out = list(prompt_token_ids), []
        for step in range(max_new_tokens):
            row = self.lm.logits(ids)[-1]
            pred = int(row.argmax())
            if next_input is not None:
                out.append(pred)
                ids.append(next_input(step, pred))
            else:
                tok = int(torch.multinomial(torch.softmax(row / temperature, -1), 1, generator=g))
                out.append(tok)
                ids.append(tok)
        return out

    def reset(self):
        pass

    def teardown(self):
        self.torn_down = True


class FakeTokenizer:
    def apply_chat_template(self, messages, add_generation_prompt=True, tokenize=True, **kw):
        text = messages[-1]["content"]
        return [1] + [ord(c) % VOCAB for c in text[:20]] + [2]


CANDIDATES = {"ref": {}, "equal": {"noise": 1e-4}, "bad": {"quant": 0.5}}


def build(model_dir, mesh_device, **kw):
    return FakeGenerator(TinyLM(**kw))


def args_for(tmp_path, *extra):
    cand = tmp_path / "candidates.json"
    cand.write_text(json.dumps(CANDIDATES))
    argv = ["--model-dir", str(tmp_path), "--candidates", str(cand), "--num-texts", "8",
            "--max-new-tokens", "200", "--min-answer-tokens", "100", "--decode-tokens", "32",
            "--decode-texts", "2", "--sampling", '{"temperature": 1.0}', "--hf-positions", "0", *extra]
    return p1.finalize_args(p1.build_parser().parse_args(argv))


def result(tmp_path):
    return json.loads((tmp_path / "doc/eval_readiness/precision_reasoning.json").read_text())


def checks(body):
    return {c["id"]: c for c in body["checks"]}


def test_bad_candidate_is_caught_and_equal_candidate_passes(tmp_path):
    code = p1.run(args_for(tmp_path, "--selected", "bad"), build_generator=build, tokenizer=FakeTokenizer())
    body = result(tmp_path)
    c = checks(body)
    assert code == 2
    assert c["positions_scored"]["status"] == "pass" and c["positions_scored"]["value"] >= 1000
    assert c["selected_within_noise"]["status"] == "fail"
    assert body["data"]["candidates"]["bad"]["within_noise"] is False
    assert body["data"]["candidates"]["equal"]["within_noise"] is True
    assert body["data"]["text"]["count"] == 8
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_CRITICAL

    # Second run reuses the cached text and scores; selecting "equal" passes the gate.
    calls = []

    def counting_build(**kw):
        calls.append(kw)
        return build(**kw)

    code = p1.run(args_for(tmp_path, "--selected", "equal"), build_generator=counting_build, tokenizer=FakeTokenizer())
    assert code == 0 and not calls
    assert checks(result(tmp_path))["selected_within_noise"]["status"] == "pass"
    assert checks(result(tmp_path))["decode_top1_within_noise"]["status"] == "pass"
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_PASS


def test_waiver_downgrades_required_to_fit_loss(tmp_path):
    p1.run(args_for(tmp_path, "--selected", "bad"), build_generator=build, tokenizer=FakeTokenizer())
    (tmp_path / "doc/eval_readiness/limitations.json").write_text(json.dumps({"items": [{
        "probe": "precision_reasoning", "check": "selected_within_noise",
        "reason": "required to fit: BFP8 does not fit the full-context KV budget",
        "evidence": ["doc/eval_readiness/precision_reasoning.json"], "original_model": "not_run", "fix_hours": 2.0}]}))
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_ADVISORY


def test_too_few_positions_is_critical(tmp_path):
    p1.run(args_for(tmp_path, "--num-texts", "2", "--selected", "equal"), build_generator=build, tokenizer=FakeTokenizer())
    c = checks(result(tmp_path))
    assert c["positions_scored"]["status"] == "fail" and c["positions_scored"]["waivable"] is False


def test_text_file_and_hf_slice(tmp_path):
    lm = TinyLM()
    g = torch.Generator().manual_seed(3)
    rows = []
    for i in range(10):
        prompt = [1, 5 + i, 2]
        rows.append({"prompt_token_ids": prompt, "completion_token_ids": torch.randint(0, VOCAB, (150,), generator=g).tolist()})
    tf = tmp_path / "traces.jsonl"
    tf.write_text("".join(json.dumps(r) + "\n" for r in rows))

    class HF:
        def __call__(self, input_ids):
            return SimpleNamespace(logits=lm.logits(input_ids[0].tolist())[None])

    args = args_for(tmp_path, "--text-file", str(tf), "--hf-positions", "300", "--hf-model", "fake/hf", "--selected", "equal")
    code = p1.run(args, build_generator=build, tokenizer=None, hf_loader=lambda name: HF(), hf_fit=lambda name: None)
    body = result(tmp_path)
    c = checks(body)
    assert code == 0, body
    assert c["reference_vs_hf"]["status"] == "pass"
    assert body["data"]["hf"]["positions"] >= 300 and body["data"]["hf"]["kl_x1000"] < 0.01
    assert body["data"]["text"]["count"] == 8  # --num-texts caps the file
    assert (tmp_path / p1.TEXT_CACHE).is_file()

    p1.run(args, build_generator=build, tokenizer=None, hf_fit=lambda name: "HF weights 900 GiB do not fit")
    c = checks(result(tmp_path))
    assert c["reference_vs_hf"]["status"] == "skip" and "do not fit" in c["reference_vs_hf"]["detail"]


def test_within_noise_uses_two_se_or_floor():
    base = {"top1_agree": 0.97, "dtop1_text": 0.0, "positions": 2000}
    assert p1.within_noise({**base, "dnll": 0.004, "dnll_se": 0.001}, nll_floor=0.01, kl_max_x1000=15)[0]
    assert not p1.within_noise({**base, "dnll": 0.004, "dnll_se": 0.001}, nll_floor=0.0, kl_max_x1000=15)[0]
    assert p1.within_noise({**base, "dnll": 0.03, "dnll_se": 0.02}, nll_floor=0.01, kl_max_x1000=15)[0]
    assert not p1.within_noise({**base, "dnll": 0.0, "dnll_se": 0.001, "kl_x1000": 40.0}, nll_floor=0.01, kl_max_x1000=15)[0]


def test_cluster_se_matches_independent_case_and_coarse_kl_is_lower_bound():
    v = torch.tensor([1.0, -1.0] * 50)
    mean, se = p1.mean_se(v, p1.clusters_for([10] * 10))
    assert mean == 0.0 and se == 0.0  # each text is balanced
    mean, se = p1.mean_se(torch.tensor([1.0] * 50 + [0.0] * 50), p1.clusters_for([50, 50]))
    assert mean == 0.5 and se > 0
    p = torch.log_softmax(torch.randn(4, VOCAB), -1)
    q = torch.log_softmax(torch.randn(4, VOCAB), -1)
    full = (p.exp() * (p - q)).sum(-1)
    idx = p.topk(8, -1).indices
    coarse = p1.coarse_kl(p.gather(1, idx), q.gather(1, idx))
    assert torch.all(coarse <= full + 1e-6) and torch.all(coarse >= 0)


def test_degenerate_loop_is_detected():
    assert p1.is_degenerate([3, 4, 5] * 300)
    assert not p1.is_degenerate(torch.randint(0, 1000, (600,), generator=torch.Generator().manual_seed(1)).tolist())


def test_bundled_prompts_and_candidate_limit(tmp_path):
    prompts = p1.load_prompts(p1.DEFAULT_PROMPTS)
    assert len(prompts) == 30 and len({p["id"] for p in prompts}) == 30
    f = tmp_path / "c.json"
    f.write_text(json.dumps({str(i): {} for i in range(6)}))
    with pytest.raises(SystemExit, match="max-candidates"):
        p1.load_candidates(f, 5)


def test_sampling_params_object_is_built_from_the_generator_module():
    import dataclasses

    @dataclasses.dataclass
    class SamplingParams:
        temperature: float = 0.0
        top_p: float = 1.0
        seed: int = 0

    seen = []

    class ObjGen(FakeGenerator):
        def generate(self, prompt_token_ids, max_new_tokens, *, next_input=None, enable_trace=True, sampling_params=None):
            seen.append(sampling_params)
            return FakeGenerator.generate(self, prompt_token_ids, max_new_tokens, temperature=sampling_params.temperature,
                                          seed=sampling_params.seed)

    sys.modules[ObjGen.__module__].SamplingParams = SamplingParams
    try:
        texts = p1.generate_texts(ObjGen(TinyLM()), FakeTokenizer(), p1.load_prompts(p1.DEFAULT_PROMPTS)[:3], num_texts=3,
                                  max_new_tokens=120, sampling={"temperature": 0.8, "top_k": 64}, template_kwargs={},
                                  min_answer_tokens=100, max_text_tokens=4096, log=lambda m: None)
    finally:
        del sys.modules[ObjGen.__module__].SamplingParams
    assert len(texts) == 3
    assert [s.seed for s in seen] == [1000, 1001, 1002] and all(s.temperature == 0.8 for s in seen)
