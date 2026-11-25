# Correctness: defaults and the HF reference

TTNN defaults that change results while a module's semantics are being proven, and building and trusting the HF
reference, including with synthetic weights.

## TTNN defaults while proving semantics

- Start with `ttnn.bfloat16`, `ttnn.TILE_LAYOUT`, and `ttnn.DRAM_MEMORY_CONFIG` where that simplifies correctness; optimize memory layout after PCC is stable.
- Transpose 2D PyTorch linear weights before `ttnn.linear` unless the local TTNN helper already does it.
- Reshape norm weights so they broadcast over hidden width.
- Derive Q/K/V head reshapes from config, including GQA/MQA KV expansion, before optimizing.
- In `models/common` attention, Q/K weights may need HF-to-TTNN RoPE permutation handling such as `reverse_permute`; inspect the module contract and tests.

## HF reference and synthetic weights

- Prefer a layer-only HF reference over full causal LM loading.
- Inspect the decoder layer constructor, rotary/position embedding inputs, attention mask semantics, and cache API before writing the test.
- Use real weights to record tensor stats and to validate that `from_state_dict` can load a real checkpoint path when available. Treat the real HF state dict as the canonical key and shape contract. Normal CI tests should use synthetic weights generated from those stats so they do not require HF weight downloads.
- For each tensor used by the TTNN layer, store at least name, shape, dtype, mean, and std.
- Generate synthetic weights deterministically from those stats in pytest.
- Synthetic input activations should approximate the distribution entering the decoder layer after embeddings/norms, not arbitrary huge random values.
- Do not create smaller configs - replicate the real config of the reference.
- If full HF weight download is necessary to collect stats, record why partial shard loading was not practical.
- Always use real shapes even with synthetic weights.
- You must include a test with real weights and show it passes PCC >= 0.995 for each of the layer kinds.
