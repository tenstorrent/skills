# Pitfalls

Each of these cost hours to days on the reference model. Check for them before you debug.

## Device and build

- **A stale build after switching branches mid-build** left objects from two source trees in one
  library; every device open then crashed in runtime option hashing, and an unmodified main
  crashed the same way. `ninja -t clean` and rebuild before blaming the model. Never switch the
  checkout's branch while a build runs.
- **Kernel compatibility code is tied to a runtime.** The reference package's kernels carried
  workarounds for two matmul fixes missing from its pinned runtime. On a newer runtime the
  workaround-free kernels were 4% faster; on the old runtime they silently dropped a tile. Ship
  runtime and kernels together, and start a new runtime with an empty JIT cache.
- **Host-only TTNN calls still open the device stack** and take the chips' locks. Run them through
  the same chip-ownership rules as device work ($tt-device-usage).

## Traces and programs

- **A prefill length first seen after the decode capture** corrupted a RoPE slice cached in trace
  scratch memory; later prompts of that length produced garbage. **A program compiled after
  capture** can place long-lived buffers in trace scratch and hang the device. Both are
  preparation defects: warm every bucket first, run the allocation tracker
  (`TT_METAL_TRACE_ALLOC_TRACKING=1` or `ttnn.tools.trace_allocation_tracker`) and keep the
  program-cache count flat ($tt-enable-tracing).
- **Persistent L1 grows with features.** Per-forward buffers at width 8 overflowed L1 on one core;
  a router weight kept in L1 at 128 experts took 360 KB of every core and clashed with prefill
  attention's circular buffers at 2k tokens. Budget L1 per feature and width, and keep large
  constants in DRAM.

## Numerics

- **A "higher fidelity" kernel variant returned Inf** (HiFi4 in the multi-row norm). Test every
  variant against the stock op; do not assume higher fidelity is safer.
- **Summation order is part of exactness.** Summing a row's experts in id order instead of its own
  rank order flipped near-ties one ulp at a time; it showed up only in long generations. Match the
  batch-1 order wherever output must be identical.
- **An "exact" element-wise fusion** must reproduce the stock op's register width (16-bit
  destination registers on the reference model) to stay bit-identical.
- **Lower-bit formats are an accuracy question.** BFP4 experts saved 20% of the step and lost 4.7
  points of top-1 against BFP8; the best quantizer tried (per-block exponent choice by squared
  error, tt-lab's method) still missed the bar. The 120b sibling needed BFP4 for capacity and passed
  against its own stock baseline. Decide per model with the gates.
- **Random-token prompts understate draft acceptance and overstate argmax noise.** Gate on natural
  text.

## Weights and caches

- **The weight-cache key must encode memory placement.** A cached tensor is restored with the
  memory configuration it was saved with; moving a tensor from L1 to DRAM without a new key loaded
  the old placement.
- **Decode-only copies change capacity.** Re-derive the KV pool and the context contract after
  adding them, on every mesh.

## Serving

- **A path can slow a workload it does not run.** The two-chip fused decode slowed the stock
  eight-user decode by 0.2 ms per step, cause unknown. Measure every ladder row after adding a
  path, not only the rows it serves.
- **Penalties, log-probs and grammar masks** need a route to the stock sampler at every width,
  captured at warmup.
- **Speculation and prefix caching:** a cache hit means the drafter has not seen the cached part
  of the prompt; disable speculation for that request.
- **Some serving plugins rewrite large configurations to an alias architecture** (a 36-layer,
  128-expert config became its own class name). Match the alias in the bundle, or the server picks
  another adapter.

## Measurement and reporting

- **Timing under load.** A two-chip step measured at load 6 read 5.57 ms and was not evidence;
  the quiet-box number decided.
- **"Faster than X"** at different formats is a bytes comparison, not an engine comparison. The
  reference model read 3.8 GB per token where a BF16-attention runner read 5.1 GB; at matched
  formats the two were about even. Say which formats differ.
- **Quality scores drift with the date** when the chat template includes it. Pair on identical
  prompts on the same day, or the comparison measures the calendar.
