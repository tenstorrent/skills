---
name: tt-vllm-serving-review
description: Reviews the vLLM and tt-inference-server serving path, including generator contracts, explicit decode reload commands, plugin registration, and TT data-parallel layouts. Use when reviewing generator_vllm.py, vLLM plugin registration, or serving configuration.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/vllm-tt-plugin
      ref: cacf1e7a867ae7b636d173ed42f8b409418f59a5
      path: docs/DECODE_RELOAD_CONTRACT.md
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

## Decode reload commands

For `decode_input_update_contract = 1`, inspect all four commands from caller to model:

- `reload_inputs` copies all forward inputs, including page tables.
- `reload_page_table` copies only page tables and preserves token, position, and RoPE state.
- `reload_sampling_params` uploads sampling settings, including seeds.
- `reset_sampling_state` rebuilds penalty and RNG state. It requires a full input reload.

The plugin chooses reloads. Flag model-side heuristics that override these commands.
Host tokens and positions can be stale when `reload_inputs=False`; they must not reset inputs
or seed counters. A full reload and a page-table-only reload are mutually exclusive.
For resident decode, require evidence for all three input modes and mode or trace-buffer
transitions. A full-reload-only adapter can use version 1 with async support false; test
full reloads and clear rejection of unsupported resident commands instead.

Contract version does not imply `supports_async_decode`. Async support needs split readback
returning `(host_output, read_events)`, device token feedback, one position advance per decode,
and independent page-table refresh. Readback must not sample or change state.

Check `slot_remap[i] = j` before slot state is read. Every slot-bound subsystem consumes it once,
including a dormant sampler during host sampling. A full input reload is not a remap. New slot
ownership comes from prefill placement. Partial prefill must preserve unlisted live slots and
their sampling parameters, seeds, and penalty history. A seed reset must initialize device seeds
even when both cached and requested seeds are `None`.

Read `docs/DECODE_RELOAD_CONTRACT.md` in the selected standalone plugin checkout for lifecycle
and DP details. Missing or zero contract version uses the legacy call shape. Do not flag a
legacy adapter merely for lacking the new keywords, or claim an inherited marker proves that
an override implements them.

## Serving dependency

New bring-up uses `tenstorrent/vllm-tt-plugin` with its recommended upstream vLLM version.
Check the selected installation instructions, imports, and source commits. For an explicitly
maintained fork, inspect its caller contract separately; do not assume either API applies.

## Perf evidence works differently here

**vLLM and optimised-vLLM stages deliberately skip Tracy, `tt-perf-report`, and device-profiler
collection.** Do not ask for device-level profiling evidence from a serving-stage change, and do not
treat its absence as a gap — see `tt-perf-claim-review`. Serving claims rest on serving-level
metrics.

## Severity

A contract mismatch that produces wrong output for real traffic is `MUST-FIX`. Silent fallback where
an explicit error belongs is `MUST-FIX`. A registration path that works in tests but not in the
server is `MUST-FIX` — it will not be caught downstream. Convention drift is `SHOULD-FIX`.
