---
name: tt-trace-review
description: Reviews trace capture and replay safety — nothing host-side inside capture, program-cache warmup with exact signatures including scalar argument values, no allocation after capture, and device-owned autoregressive state. Use when reviewing changes touching begin_trace_capture, execute_trace, generator decode paths, or program-cache warmup.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/tt-enable-tracing/SKILL.md
      branch: agentic-research/fast-models-fast
---

# Trace capture and replay review

Assumes `tt-review-core`. Trace bugs present as an abort during capture or as a silently wrong steady
state, and both are cheap to catch by reading the capture region.

## The trace-safe shape

1. Build weights, caches, page tables, semaphores, persistent CCL buffers, and lazy module state
   **before** capture.
2. Allocate stable device input tensors before capture.
3. Run one warm compile with the same shapes and mode so every op is compiled.
4. `begin_trace_capture`.
5. Call a **device-only** forward that consumes the stable device tensors.
6. `end_trace_capture`.
7. Per replay: update the stable device inputs *outside* capture, then `ttnn.execute_trace`.

Review a capture region against this shape. Most findings are a step performed in the wrong place.

## Nothing host-side inside capture

Flag any of these inside the captured region:

- `ttnn.from_torch(..., device=...)`, `ttnn.as_tensor(..., device=...)`, `ttnn.to_device`,
  `ttnn.copy_host_to_device_tensor`
- `ttnn.to_torch`, `.cpu()`, `get_device_tensors` followed by host conversion, full-logits host
  composition
- `ttnn.synchronize_device`, event waits, explicit reads
- Lazy weight loading, model-cache loads, first-use module initialisation
- Resetting KV cache, page tables, semaphores, or sampling state
- **Python decisions that change the op sequence, shape, memory config, or code path**

The last one is the subtle one: a Python `if` inside capture does not fail — it bakes one branch
into the trace. The trace then replays that branch forever, including for inputs that should have
taken the other. Where a path needs a host decision, it must be made before capture and bound at
construction.

## Program-cache warmup

**Trace capture cannot compile.** A cache miss inside capture forces a kernel build, which issues a
host-to-device write and aborts with `Writes are not supported during trace capture`. Every op in
the traced region must already be compiled with the *exact* signature it will have during capture.

- Warm with the same shapes, dtypes, layouts, memory configs and mode, driving the identical op
  sequence.
- **The signature includes argument values you would not expect.** The integer `begins` / `ends` /
  `step` passed to `ttnn.slice` are compile-time constants baked into the program hash — slicing at
  a different offset, length, or start-tile alignment is a *different program* needing its own
  warmup. When in doubt, warm with the same argument **values**, not just the same tensor shapes.
  This is the most commonly missed case and worth checking explicitly on any diff that adds a slice.
- **Warm state-update ops too.** `ttnn.plus_one`, page and position updates, sampler trace setup,
  persistent output buffer allocation. They are easy to forget because they are not "the model", but
  they compile programs.
- If warmup mutates persistent trace inputs — token, position, RoPE index, page table, KV-cache
  state — those must be reset to the exact intended capture state immediately before
  `begin_trace_capture`.

## Autoregressive state stays on device

Token feedback belongs in the traced device path. Flag a sampled token read back to host and used to
reconstruct the next input — that defeats the trace.

The steady-state step should be trace replay plus the minimum caller-visible readback. Rebuilding
tokens, positions, RoPE indices, masks, or page tables on the host every token is an **incomplete
tracing implementation**, not merely a slow one. Advance device-owned state inside the captured
graph where possible. Host refresh belongs at request boundaries, explicit reset, or genuine
scheduler-owned input changes.

## Do not trace the generator directly

Unless already proven trace-safe, the generator should be split: host-side input preparation, an
explicit copy to stable device tensors, a device-only forward used for both warm compile and
capture, and a traced step that refreshes inputs and executes. A diff that wraps a high-level
generator method in capture is a finding even if it happens to work today.

## Correctness of replay

PCC must be checked **from replay output**, not from the warm compile call. A test that validates the
warm call and then traces has verified the thing it is not shipping. This is easy to miss in review
because both calls look identical at the call site — check which output the assertion consumes.

## Severity

Anything that aborts capture is `MUST-FIX`. A baked-in Python branch or host-side token feedback is
`MUST-FIX` — silently wrong or silently slow. Host refresh that could move on-device is
`SHOULD-FIX`.
