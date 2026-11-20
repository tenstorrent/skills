---
name: ttnn-knowledge
description: What earlier TTNN model bringups on Blackhole (QB2) spent time finding out, by topic - memory and L1, numerics, prefill and KV, tracing, multi-chip and CCL, MoE, performance work, serving and evals, environment. Read the topic that matches the work in front of you; each entry is a heads-up, not a rule.
---

# TTNN knowledge

Facts that cost earlier bringups hours, written so the next agent does not rediscover them. Each entry says what
was seen, why it happened, and what helped. They were observed on tt-metal of October and November 2026 on
Blackhole; when one matters for a decision, check it against current main.

Read a topic when the work reaches it:

| Topic | When |
|---|---|
| [memory](topics/memory.md) | an op fails on L1 or CB space, a dtype, batch, grid or KV change breaks a working config, modules are combined |
| [numerics](topics/numerics.md) | accuracy looks fine by PCC but outputs drift, loop or depend on batch or slot |
| [prefill and KV](topics/prefill-kv.md) | chunked or resumed prefill, paged attention, prefix reuse, long context |
| [tracing](topics/tracing.md) | trace capture, warmup, allocations or compiles after capture |
| [multi-chip and CCL](topics/multichip.md) | sharding over chips, collectives, fused CCL ops, Blackhole grid sizes |
| [MoE](topics/moe.md) | routed experts |
| [performance work](topics/perf.md) | profiling, comparing measurements, kernel edits, host-side cost |
| [serving and evals](topics/serving-evals.md) | the vLLM plugin, sampling, eval harnesses and their infrastructure |
| [environment](topics/environment.md) | containers, sandboxes, caches, small API traps |

Device handling (timeouts, resets, shared boards) is in [tt-device-usage](../tt-device-usage/SKILL.md).
