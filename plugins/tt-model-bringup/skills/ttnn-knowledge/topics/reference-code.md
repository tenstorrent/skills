# Reference code in tt-metal

Working implementations worth reading before designing a module. Check them against the target revision; paths move.

- `models/common/modules/attention/attention_1d.py`: general config-first attention with prefill/decode split, paged KV cache, trace-friendly current-position tensors, Q/K norm hooks, and CCL integration.
- `models/common/modules/mlp/mlp_1d.py`, `models/common/modules/rmsnorm/rmsnorm_1d.py`, and `models/common/modules/rope/rope_1d.py`: common patterns for decode-vs-prefill memory layout, on-device RoPE embedding, and local/distributed norm paths.
- `models/common/modules/lazy_weight.py`: setup-time TTNN tensor conversion and deterministic cache-key pattern without runtime torch imports.
- `models/tt_transformers/tt/decoder.py`: standard transformer block composition and residual order.
- `models/tt_transformers/tt/generator.py` and `models/tt_transformers/tt/model.py`: production trace capture/replay, stable trace input tensors, decode preparation, and vLLM-facing contracts.
- `models/tt_transformers/tt/generator_vllm.py`: paged KV-cache allocation, per-layer page-table routing, and hybrid attention contracts.
- `models/demos/gpt_oss/tt/layer.py` and `models/demos/gpt_oss/tt/attention/`: model-specific standard attention with paged prefill/decode and MoE-adjacent tests.
- `models/demos/gpt_oss/tt/experts/README.md`, `models/demos/gpt_oss/tt/experts/decode.py`, `models/demos/gpt_oss/tt/experts/prefill.py`, `models/demos/gpt_oss/tt/experts/weights.py`, `models/demos/gpt_oss/tt/experts/config.py`, and `models/demos/gpt_oss/tt/topk.py`: default MoE active-expert pattern using `ttnn.sparse_matmul`.
- `models/demos/gemma4/tt/layer.py` and `models/demos/gemma4/tt/attention/`: strong examples for many norms, layer scales, sliding/full layer types, shared KV, and model-specific RoPE.
- `models/demos/deepseek_v3/tt/decoder_block/` and `models/demos/deepseek_v3/tt/mla/`: examples for dense-vs-MoE decoder kinds, MLA, row-sharded behavior, and specialized paged-cache updates.
- `tech_reports/LLMs/llms.md` and `tech_reports/LLMs/vLLM_integration.md`: TTNN LLM conventions for paged cache, prefill/decode shape differences, tracing, and vLLM integration.
