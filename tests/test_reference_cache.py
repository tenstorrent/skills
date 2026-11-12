"""CPU tests for the readiness HF reference cache (no device, no network, tiny fakes)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "tt-model-bringup" / "runtime"
sys.path.insert(0, str(RUNTIME))

from readiness_check import generate, reference_cache, run_autoregressive  # noqa: E402
from readiness_check.reference_cache import ReferenceCache, cache_dir, model_revision  # noqa: E402
from readiness_check.schema import load_reference  # noqa: E402

VOCAB = 50


class FakeTokenizer:
    eos_token_id = 1
    bos_token_id = 0
    pad_token_id = None
    unk_token_id = 3
    eot_token_id = None

    def __len__(self):
        return VOCAB

    def encode(self, text, add_special_tokens=True):
        ids = [4 + (ord(ch) % (VOCAB - 4)) for ch in text]
        return ([self.bos_token_id] if add_special_tokens else []) + ids

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(str(int(i)) for i in ids)

    def get_vocab(self):
        return {}

    def convert_tokens_to_ids(self, token):
        return self.unk_token_id


class FakeModel(torch.nn.Module):
    """Deterministic next-token logits: favours (last token + 1) mod VOCAB."""

    def __init__(self):
        super().__init__()
        self.config = type("Cfg", (), {"eos_token_id": 1})()

    def forward(self, input_ids):
        logits = torch.nn.functional.one_hot((input_ids + 1) % VOCAB, VOCAB).float() * 10.0
        logits = logits + torch.arange(VOCAB).float() * 1e-3
        return type("Out", (), {"logits": logits})()

    def generate(self, input_ids, attention_mask=None, max_new_tokens=1, **_kwargs):
        seq = input_ids
        for _ in range(max_new_tokens):
            nxt = self.forward(seq).logits[:, -1].argmax(-1, keepdim=True)
            seq = torch.cat([seq, nxt], dim=1)
        return seq


@pytest.fixture
def hf_dir(tmp_path):
    model = tmp_path / "hf_model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type": "fake"}')
    (model / "tokenizer.json").write_text('{"v": 1}')
    (model / "model.safetensors").write_bytes(b"weights-v1")
    return model


@pytest.fixture
def fake_hf(monkeypatch):
    calls = {"model": 0}

    class Tok:
        @staticmethod
        def from_pretrained(*_a, **_k):
            return FakeTokenizer()

    class Mdl:
        @staticmethod
        def from_pretrained(*_a, **_k):
            calls["model"] += 1
            return FakeModel()

    for module in (generate, run_autoregressive):
        monkeypatch.setattr(module, "AutoTokenizer", Tok)
        monkeypatch.setattr(module, "AutoModelForCausalLM", Mdl)
    monkeypatch.delenv(reference_cache.CACHE_ENV, raising=False)
    return calls


def test_cache_dir_default_override_and_disable(tmp_path, monkeypatch):
    monkeypatch.delenv(reference_cache.CACHE_ENV, raising=False)
    assert cache_dir(tmp_path) == tmp_path / "readiness" / "reference_cache"
    assert cache_dir(None) is None
    monkeypatch.setenv(reference_cache.CACHE_ENV, str(tmp_path / "elsewhere"))
    assert cache_dir(tmp_path) == tmp_path / "elsewhere"
    monkeypatch.setenv(reference_cache.CACHE_ENV, "off")
    assert cache_dir(tmp_path) is None


def test_local_revision_tracks_weights_and_config(hf_dir):
    first = model_revision(str(hf_dir))
    assert first and first.startswith("local:")
    assert model_revision(str(hf_dir)) == first
    (hf_dir / "model.safetensors").write_bytes(b"weights-v2-longer")
    second = model_revision(str(hf_dir))
    assert second != first
    (hf_dir / "config.json").write_text('{"model_type": "fake", "x": 1}')
    assert model_revision(str(hf_dir)) != second


def test_unknown_hub_revision_bypasses_cache(monkeypatch):
    monkeypatch.setattr(reference_cache, "_resolve_hub_snapshot", lambda _id: None)
    assert model_revision("org/not-cached-model") is None
    key = ReferenceCache.make_key(
        kind="x", hf_model_id="org/m", revision=None, tokenizer="t", prompt="p", settings={}
    )
    assert key is None


def test_hub_revision_uses_snapshot_commit(tmp_path, monkeypatch):
    snap = tmp_path / "models--org--m" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    monkeypatch.setattr(reference_cache, "_resolve_hub_snapshot", lambda _id: snap)
    assert model_revision("org/m") == "hub:abc123"


def test_autoregressive_hf_reference_cached_and_identical(tmp_path, hf_dir, fake_hf, monkeypatch):
    calls = {"n": 0}
    real = run_autoregressive._hf_generate_greedy

    def counting(**kwargs):
        calls["n"] += 1
        return real(**kwargs)

    monkeypatch.setattr(run_autoregressive, "_hf_generate_greedy", counting)
    model_dir = tmp_path / "autoport"
    args = dict(
        model_dir=model_dir,
        hf_model_id=str(hf_dir),
        tokenizer=FakeTokenizer(),
        prompt_token_ids=[0, 5, 6, 7],
        max_new_tokens=6,
        device=torch.device("cpu"),
    )
    first = run_autoregressive._cached_hf_greedy(**args)
    second = run_autoregressive._cached_hf_greedy(**args)
    assert first == second and calls["n"] == 1
    cache_root = model_dir / "readiness" / "reference_cache"
    assert (cache_root / ".gitignore").read_text().strip() == "*"
    assert len(list(cache_root.glob("hf_greedy-*.key.json"))) == 1

    # Any key component change is a miss.
    run_autoregressive._cached_hf_greedy(**{**args, "max_new_tokens": 7})
    run_autoregressive._cached_hf_greedy(**{**args, "prompt_token_ids": [0, 5, 6, 8]})
    assert calls["n"] == 3
    (hf_dir / "model.safetensors").write_bytes(b"new-weights")
    run_autoregressive._cached_hf_greedy(**args)
    assert calls["n"] == 4

    # Disabled cache recomputes and gives the same tokens.
    monkeypatch.setenv(reference_cache.CACHE_ENV, "off")
    assert run_autoregressive._cached_hf_greedy(**args) == first
    assert calls["n"] == 5


@pytest.mark.parametrize("source", ["text", "book"])
def test_generate_reference_cache_hit_is_byte_identical(tmp_path, hf_dir, fake_hf, monkeypatch, source):
    monkeypatch.setattr(generate, "_load_book_text", lambda: "It was the best of times, it was the worst of times. " * 4)
    model_dir = tmp_path / "autoport"
    common = dict(
        hf_model_id=str(hf_dir),
        prompt_len=8,
        gen_len=5,
        top_k=4,
        prompt_source=source,
        prompt="hello there" if source == "text" else None,
        device=torch.device("cpu"),
    )

    plain = generate.generate_reference(output_path=tmp_path / "plain.refpt", **common)
    assert fake_hf["model"] == 1
    first = generate.generate_reference(output_path=tmp_path / "a.refpt", model_dir=model_dir, **common)
    assert fake_hf["model"] == 2
    second = generate.generate_reference(output_path=tmp_path / "b.refpt", model_dir=model_dir, **common)
    assert fake_hf["model"] == 2, "cache hit must not load the HF model"
    assert first.read_bytes() == second.read_bytes()

    ref_plain, ref_hit = load_reference(plain), load_reference(second)
    assert ref_plain.k == ref_hit.k and ref_plain.hf_model_id == ref_hit.hf_model_id
    assert ref_plain.token_ids_meta == ref_hit.token_ids_meta
    for a, b in zip(ref_plain.entries, ref_hit.entries):
        assert a.prompt_text == b.prompt_text and a.tf_prompt_len == b.tf_prompt_len
        assert torch.equal(a.prompt_tokens, b.prompt_tokens)
        assert torch.equal(a.generated_tokens, b.generated_tokens)
        assert torch.equal(a.topk_tokens, b.topk_tokens)

    generate.generate_reference(output_path=tmp_path / "c.refpt", model_dir=model_dir, **{**common, "top_k": 3})
    assert fake_hf["model"] == 3


def test_generate_book_path_prefers_ttt_compat(tmp_path, monkeypatch):
    import bz2

    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="ttt_compat"):
        generate._load_book_text()
    legacy = tmp_path / "models" / "tt_transformers" / "tests" / "tale-of-two-cities.txt.bz2"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(bz2.compress(b"legacy"))
    assert generate._load_book_text() == "legacy"
    current = tmp_path / "models" / "ttt_compat" / "tests" / "tale-of-two-cities.txt.bz2"
    current.parent.mkdir(parents=True)
    current.write_bytes(bz2.compress(b"current"))
    assert generate._load_book_text() == "current"
