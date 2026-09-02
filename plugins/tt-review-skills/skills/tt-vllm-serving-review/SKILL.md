---
name: tt-vllm-serving-review
description: Reviews the vLLM and tt-inference-server serving path — generator_vllm.py contracts, plugin registration, the tt_data_parallel ambiguity, and TT-fork branch and config conventions. Use when reviewing changes to generator_vllm.py, vLLM plugin registration, or serving configuration.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/vllm-integration/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: knowledge/recipes/vllm
---

# vLLM serving path review

Assumes `tt-review-core`. Reviews the boundary between a model and the serving stack, where the
recurring failure is a contract mismatch rather than a bug inside either side.

## "Data parallel" is the trap

`tt-review-core` carries this guard; it matters most here, so restate it at the site.

vLLM's `data_parallel_size` / `tt_data_parallel` may mean the SDPA/KV-cache data-parallel degree.
tt-metal code often means mesh-local structure — input mesh rows, attention weight copies. **These
are different axes with the same name.**

Before flagging any relationship among `tt_data_parallel`, `max_batch_size`, `batch_size_per_row`,
mesh rows, mesh columns, and mesh world size, **read the active caller and launch contract.** This
mismatch has produced confident, wrong review comments. If the contract is not determinable from the
diff, that is an `Unresolved` item and a severity downgrade — not a guess.

## The generator contract

`generator_vllm.py` sits between vLLM's expectations and the model's device path. Check both
directions:

- **Shapes and dtypes at the boundary** match what vLLM will actually send, not what the test
  harness sends.
- **Page table and KV-cache semantics** agree with the model's own indexing. See
  `tt-model-bringup-review` on logical batch versus tile padding — a serving path that reinterprets
  padded rows as users is the same bug at a different layer, and here it is reachable from real
  traffic.
- **Sequence-length and context handling** at boundaries: the first token, the maximum context, and
  the transition from prefill to decode.
- **Error paths.** What happens on an unsupported request shape? Per `tt-review-core`, an explicit
  error at the boundary is correct; a silent fallback that produces wrong output is not.

## Plugin registration

Registration is easy to get subtly wrong in a way that fails at import or, worse, silently registers
nothing and falls back to a default implementation. Check that the entry point name matches what
vLLM looks up, and that registration happens on the import path actually taken when the server
starts — not only under a test import.

## TT-fork conventions

vLLM here is a Tenstorrent fork. Branch and config conventions are load-bearing: a change pinning
the wrong branch, or assuming upstream behaviour the fork has changed, breaks at deploy rather than
in CI. Where a diff pins or bumps a fork reference, check it against the convention in the
surrounding config rather than assuming upstream semantics.

## Perf evidence works differently here

**vLLM and optimised-vLLM stages deliberately skip Tracy, `tt-perf-report`, and device-profiler
collection.** Do not ask for device-level profiling evidence from a serving-stage change, and do not
treat its absence as a gap — see `tt-perf-claim-review`. Serving claims rest on serving-level
metrics.

## Severity

A contract mismatch that produces wrong output for real traffic is `MUST-FIX`. Silent fallback where
an explicit error belongs is `MUST-FIX`. A registration path that works in tests but not in the
server is `MUST-FIX` — it will not be caught downstream. Convention drift is `SHOULD-FIX`.
