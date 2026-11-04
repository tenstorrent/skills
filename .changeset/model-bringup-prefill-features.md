---
"tt-model-bringup": minor
---

Require prefix caching and batched prefill from every served text model. The generator
contract gains `start_pos` on `prefill_forward` and a `prefill_logits_continuation` driver;
a new `run_prefill_resume_check` runner compares resumed prefill with full prefill at block,
lcm, tile-unaligned, sliding-window and prefix-cache-hit starts and requires its negative
controls to fail. The vLLM readiness runner gains `--enable-prefix-caching`, a
`prefix_cache` stage (metrics counter deltas, TTFT, first-token and greedy agreement,
shared-prefix hit rate) and a `prefill_burst` stage with a serial-reference TTFT ratio, and
writes `vllm_run_summary.json`. `check_prefill_features.py` gates stages 9 and 10 on that
evidence; `check_serving_contract.py` accepts `supports_prefix_caching: not_supported` only
for a `block_output` or `recurrent_state` exclusion. The skills describe the continuation
invariants, the packed and equal-length batched-prefill designs, the warmup rule, and the
review triggers in `vllm-integration/references/prefill-features.md`.
