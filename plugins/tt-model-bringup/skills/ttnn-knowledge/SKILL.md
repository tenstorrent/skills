---
name: ttnn-knowledge
description: What earlier TTNN model bringups on Blackhole (QB2) spent time finding out, by topic - memory and L1, numerics, prefill and KV, tracing, multi-chip and CCL, MoE, graph fusion, performance work, serving and evals, environment. Read the topic that matches the work in front of you; each entry is a heads-up, not a rule.
---

# TTNN knowledge

Facts that cost earlier bringups hours, written so the next agent does not rediscover them. Each entry says what
was seen, why it happened, and what helped. They were observed on tt-metal of October and November 2026 on
Blackhole; when one matters for a decision, check it against current main. Some topics (fusion, trace and multiple
command queues, parts of tracing and multichip) are reference material carried over from stage skills.

Read a topic when the work reaches it:

| Topic | When |
|---|---|
| [memory](topics/memory.md) | an op fails on L1 or CB space, a dtype, batch, grid or KV change breaks a working config, modules are combined |
| [numerics](topics/numerics.md) | accuracy looks fine by PCC but outputs drift, loop or depend on batch or slot; choosing a precision policy |
| [prefill and KV](topics/prefill-kv.md) | chunked or resumed prefill, paged attention, prefix reuse and batched prefill, long context |
| [tracing](topics/tracing.md) | trace capture, warmup, the generator split, allocations or compiles after capture, replay bugs |
| [trace and multiple command queues](topics/trace-multi-cq.md) | trace APIs for fixed-shape models, overlapping input writes with two command queues |
| [multi-chip and CCL](topics/multichip.md) | sharding over chips, TP patterns and 2D plans, distributed norms, collectives, fused CCL ops, Blackhole grid sizes |
| [MoE](topics/moe.md) | routed experts and their precision, expert placement on a mesh |
| [performance work](topics/perf.md) | profiling, comparing measurements, kernel edits, host-side cost, roofline accounting |
| [performance advice](topics/perf-advice.md) | optimizing a decoder: packing, attention and residual contracts, CCL topology, LM head and sampling (OPT-001 to OPT-015) |
| [graph fusion](topics/fusion.md) | replacing op sequences with fused ops, graph rewrites, folding ops; worked examples |
| [matmul and precision](topics/matmul-precision.md) | matmul program configs and geometry, sparse matmul `nnz`, dtype, fidelity and compute kernel configs |
| [serving and evals](topics/serving-evals.md) | the vLLM plugin, sampler routing, async decode, eval harnesses and their infrastructure, slow runs |
| [reference code](topics/reference-code.md) | before designing a module: working implementations in tt-metal |
| [correctness](topics/correctness.md) | TTNN defaults while proving semantics, HF reference layers, synthetic weights |
| [profiling](topics/profiling.md) | measuring traced runs, `tt-perf-report`, watcher |
| [environment](topics/environment.md) | containers, sandboxes, caches, small API traps |

Device handling (timeouts, resets, shared boards) is in [tt-device-usage](../tt-device-usage/SKILL.md).
