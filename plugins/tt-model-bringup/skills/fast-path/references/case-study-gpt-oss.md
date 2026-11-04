# Case study: gpt-oss-20b and gpt-oss-120b on QuietBox 2

The method in this skill comes from making `openai/gpt-oss-20b` (24 layers, 32 experts, top-4,
MXFP4 checkpoint) fast on a QuietBox 2, then applying it to `openai/gpt-oss-120b`. All numbers
were measured on one box, batch 1 at a 128-token context unless stated.

## Starting point

The served package (an autoport built with the stage workflow) decoded one user at 11.34 ms per
token on four chips (88 tok/s), 13.74 ms on two and 20.62 ms on one: 18% and 38% of DRAM peak.
Every layer ran about 50 TTNN ops, and the routed-expert matmuls stalled near 220 GB/s.

tenstorrent/tt-metal #60161 (merged) rebuilt the four-chip batch-1 decode from `generic_op`
programs: a one-op all-reduce, expert streams that read only the routed experts, explicit program
configs, a boundary program, and a streamed LM head with on-device top-32 sampling. Its change
history went 18.35 → 2.42 ms with 4-bit experts.

## What was done, in order

| Step | Result |
|---|---|
| Fused decode as the package's batch-1 bucket, 8-bit experts (exact from MXFP4) | 4 chips 11.34 → 3.39 ms; one user 88 → 298 tok/s; top-1 94.3 → 96.5% |
| One-chip fused decode streaming a shared expert layout | 20.62 → 9.55 ms; 49 → 105 tok/s |
| Time-to-first-token work kept bit-identical | 4k prompt 506 → 459 ms (later 392) |
| Two-chip fused decode (default on the latency profile) | 13.74 → 5.54 ms; 73 → 182 tok/s |
| Fused multi-row decode, widths 2 and 4 | 2 users 142 → 335 tok/s, 4 users 233 → 498 |
| Batched decode streaming the expert union, widths 8-32 | 32 users 1,197 → 1,761 tok/s |
| 4-bit experts with a better quantizer | rejected: best 95.3% top-1 against a 96.0% bar |
| Opt-in deterministic prefill all-reduce | bit-identical prefills at no measured cost |
| gpt-oss-120b with the same decode (4-bit experts kept for capacity) | one user 65 → 246 tok/s; quality subsets equal or better |
| Opt-in speculative decoding (EAGLE-3, one draft, exact 2-row check) | one user 306 → 359 tok/s on real text (1.17×); 0.985× on random tokens |
| Fixed-cost cuts in the multi-row step | 4 chips 3.39 → 3.29 ms; 2-4 users +6% |

Final state on four chips: one user 306 tok/s (359 with speculation), 32 users 1,753 tok/s in
total, 4k-token first token in 392 ms, full GSM8K 87.8% and IFEval 80.2% against 86.9% and 80.2%
for the original package.

## Bytes and bandwidth

| Batch 1 | Weight formats | GB per token | Step | % of peak per chip |
|---|---|---|---|---|
| original, 4 chips | 8-bit everywhere, LM head padded | 4.2 | 11.34 ms | 18% |
| bare-metal runner, 4 chips | 8-bit experts, BF16 attention and LM head | 5.1 | 4.01 ms | 63% |
| #60161, 4 chips | 4-bit experts, 8-bit attention and LM head | 2.6 | 2.42 ms | 53% |
| fused decode, 4 chips | 8-bit everywhere, unpadded | 3.8 | 3.39 ms | 55% |
| bare-metal runner, 1 chip | 8-bit experts, BF16 attention and LM head | 5.0 | 11.01 ms | 89% |
| fused decode, 1 chip | 8-bit everywhere, unpadded | 3.9 | 9.55 ms | 81% |

The fused decode beat the bare-metal runner (tt-lab) per token by reading fewer bytes, not by
using bandwidth better; at matched formats the two would be about even. Measured with the same
protocol, the runner was more accurate token by token (98.6% vs 96.5% top-1 against fp32,
attributed to its BF16 attention and LM head) and equal on full GSM8K and IFEval with identical
prompts on the same day (86.6 vs 86.6, 79.7 vs 79.8; paired test p = 1.0). Report both readings.

## Code to read

- Four-chip batch-1 decode: `models/demos/gpt_oss/tt/fused_decode/` in tenstorrent/tt-metal.
- One-chip variant: tenstorrent/tt-metal #61225. Deterministic prefill: #61226. Trace-safety
  fixes: #60412. 8-bit expert option: #60414.
- Everything else (two-chip, multi-row, batched, prefill kernels, drafter, speculative step, the
  per-op check scripts) ships as model code in the public package
  [jashansinghTT/gpt-oss-20b-blackhole-fast](https://huggingface.co/jashansinghTT/gpt-oss-20b-blackhole-fast/tree/main/code/models/autoports/openai_gpt_oss_20b/tt):
  `fused_b1/`, `batched/`, `prefill_swiglu.py`. The 120b package is
  [jashansinghTT/gpt-oss-120b-blackhole-fast](https://huggingface.co/jashansinghTT/gpt-oss-120b-blackhole-fast).

## What carried over to 120b

The decode programs took the larger model with configuration changes: 36 layers, 128 experts, the
shared expert layout at four chips, and 4-bit experts because 8-bit ones (about 30 GB per chip)
do not fit. New work: router weights moved from L1 to DRAM (they clashed with prefill attention
at 2k tokens), the serving plugin's alias architecture, and a cache key that records placement.
The port took under a day; most of it was measurement and gates.

## How the work was run

Each lever was a lane with one goal, a bar, a time cap and a kill rule, on its own branch; an
integrator merged kept lanes with the fast checks in [validation](validation.md) and rejected one
that slowed another workload. Three rounds took about four days of machine time on one QuietBox 2.
The levers that failed (4-bit experts, a replacement router, streaming all experts at small
widths, a fused matmul + reduce-scatter that hung at 32 rows) were closed with their numbers in a
day each, which is the point of the bars.
