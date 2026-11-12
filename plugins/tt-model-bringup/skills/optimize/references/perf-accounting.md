# Performance accounting

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

## Performance Accounting

Every optimized non-vLLM decode result must reconcile three numbers from the same run:

1. Theoretical roofline: the bytes the measured path must move per token (weights at their stored dtypes plus KV-cache reads) divided by the aggregate DRAM bandwidth of the chips used.
2. Device-time decode: per-token device time from your own signposted `tt-perf-report` window.
3. End-to-end decode: warmed measured ms/token from the host.

Report all three and use the gaps to drive implementation work: end-to-end = device time + dispatch gap + host work. Remove avoidable non-device terms before accepting the result. "The device math is fast but the loop is slow" is an unfinished optimization, not a result; a large unexplained gap between device time and end-to-end usually means an untraced path, per-step synchronization, host readback, or input-refresh overhead. Only name a TTNN/runtime/API limitation after you have tried the targeted fix and have evidence that the limitation blocks the optimized path.

The roofline fraction achieved varies legitimately by architecture - modules built from many small ops sit lower - so the explanation, not a fixed percentage, is the requirement. Name the limitations precisely; they feed the ttnn improvement backlog.

When optimizing a complete model or serving path, also write `doc/<stage>/perf_summary.json` with this shape. For vLLM serving stages, set device-time fields to `null` and name the reason, for example `vllm_serving_profiler_disabled_to_protect_hardware`.

```json
{
  "workload": {"profile": "single_user_decode", "prompt_len": 128, "gen_len": 128, "batch": 1},
  "ttft_ms": 0.0,
  "decode_ms_per_token_e2e": 0.0,
  "decode_ms_per_token_device": 0.0,
  "roofline_ms_per_token_estimate": 0.0,
  "named_limitations": ["..."]
}
```

When the profile comes from a reduced-layer variant, `decode_ms_per_token_device` is the per-layer decode window times the layer count (summed per layer kind) plus the terminal path (final norm, LM head, sampling) from the same profile; the host gap is `decode_ms_per_token_e2e` minus that device figure. State the derivation and the variant beside the number.
