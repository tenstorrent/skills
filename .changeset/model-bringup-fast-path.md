---
"tt-model-bringup": minor
"tt-skills": patch
---

Add the `fast-path` skill and an optional seven-goal fast-path track that runs after stage 10.
It takes a served model toward the Blackhole DRAM roofline on QuietBox 2 meshes: a roofline and
plan, a DRAM-streaming fused batch-1 decode built from `generic_op` programs, one- and two-chip
variants, fused multi-row and batched decode, prefill work that keeps digests bit-identical, an
opt-in deterministic prefill, and opt-in speculative decoding with an exact check. Each goal writes
`doc/fast_path/<phase>.json`, which `scripts/check_fast_path.py` gates on speed, accuracy, KV
capacity, trace hygiene and release thresholds. The skill ships a roofline calculator, an
expert-union calculator and a speculation cost model, and documents the gpt-oss-20b results
that set its thresholds.
