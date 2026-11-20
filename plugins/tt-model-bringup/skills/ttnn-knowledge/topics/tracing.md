# Tracing and the program cache

**Allocation or compilation after capture.** The usual causes, in the order they were found: programs that only
some prompt lengths or offsets use (see prefill-kv); buffers created lazily on first use of a slot or on the
first decode step; reset, prefill-chunk and sampling programs never run before the decode trace was recorded. A
replayed trace then reads memory a later program overwrote, which showed up as garbage from the second request.
What helped: create persistent buffers at initialization, warm every prefill shape and every slot path before
capture, run tests with `TT_METAL_TRACE_ALLOC_TRACKING=1`, and check that the program-cache count does not grow
across a mixed-length request loop after warmup.

**A retained eager output changes the capture.** Keeping the last eager warmup output alive lowers the L1
allocator frontier; some ops pick a different program (their hash depends on available space), so capture misses
the cache or overlaps L1. Releasing warmup outputs before capture fixed it.

**End capture on every path.** An exception inside a capture body that skips `end_trace_capture` leaves capture
active, and closing the mesh then hangs. Wrap capture in try/finally and release traces in a tolerant cleanup.

**Late kernels are hard to see.** Some program variants (for example certain flash-decoding prefill paths) compile
only for shapes the warmup skipped; the allocation tracker found them, a single short test did not.

## How capture works

Capture records a static command sequence on a device or mesh; replay reuses it without per-op host dispatch.
Eager dispatch between many small ops can dominate decode even when the kernels are fast, which is why traced
decode is the measured path. The traced region is device work over stable device tensors: host-originated input
changes are copied into those tensors before replay. Outputs produced by ops inside capture are fine; host writes,
host reads and host synchronization while capture is open are not. API basics, a fixed-shape example and two
command queues: [trace and multiple command queues](trace-multi-cq.md); the correctness rules and the allocation
tracker: `tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md` in the target checkout.

Code worth reading:
- `tests/ttnn/tracy/test_trace_runs.py`: minimal capture and replay.
- `models/tt_transformers/tt/generator.py`: decode traces with host input preparation, persistent device inputs,
  replay refresh and split sampling; `models/tt_transformers/tt/model.py`: the `prepare_decode_inputs_host` and
  device-only `ttnn_decode_forward` split.
- `models/common/sampling/generator.py` and `models/common/modules/sampling/sampling_1d.py`: on-device sampling.

## Opening the device

Reserve trace space when opening, or use `trace_region_size=0` where supported (see
[trace and multiple command queues](trace-multi-cq.md)). For a mesh, set the fabric config that matches the
hardware topology before opening it ([multichip](multichip.md)):

```python
device = ttnn.open_device(device_id=0, trace_region_size=100000000)

ttnn.set_fabric_config(<fabric config for this mesh's topology>)
mesh_device = ttnn.open_mesh_device(mesh_shape=ttnn.MeshShape(1, num_chips), trace_region_size=100000000)
```

## A trace-safe structure

1. Build weights, caches, page tables, semaphores, persistent CCL buffers and lazy module state.
2. Create host input tensors (`device=None` is fine) and allocate stable device input tensors.
3. Warm every supported variant with its exact shapes and modes (below).
4. Begin capture, call a device-only forward over the stable tensors, end capture.
5. For each replay, update the stable inputs outside capture, then `ttnn.execute_trace`.

```python
trace_input = ttnn.from_torch(dummy_input, dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT,
                              device=device, memory_config=ttnn.DRAM_MEMORY_CONFIG)

compiled = model_forward(trace_input)          # warm compile outside capture
ttnn.synchronize_device(device)

trace_id = ttnn.begin_trace_capture(device, cq_id=0)
trace_output = model_forward(trace_input)
ttnn.end_trace_capture(device, trace_id, cq_id=0)

for batch in batches:
    batch_host = ttnn.from_torch(batch, dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT)
    ttnn.copy_host_to_device_tensor(batch_host, trace_input)
    ttnn.execute_trace(device, trace_id, cq_id=0, blocking=True)
```

With several variants, finish every variant's preparation before the first capture.

## Program-cache signatures

Capture cannot compile: a program-cache miss inside capture builds a kernel, issues a host-to-device write and
aborts with `Writes are not supported during trace capture`. Every op in the traced region must already be
compiled with the exact signature it has during capture.

- The warm call drives the identical op sequence with the same shapes, dtypes, layouts, memory configs and mode.
  Paged-attention and cache-fill program keys include the KV-cache tensor shape.
- Integer arguments can be compile-time constants: `begins`/`ends`/`step` of `ttnn.slice` are baked into the
  program hash, so another offset, length or start-tile alignment is another program. Tensor-valued arguments
  avoid this only where the op supports them. When unsure, warm with the same argument values, not only shapes.
  More such keys: [prefill and KV](prefill-kv.md).
- State-update ops compile too: `ttnn.plus_one`, page and position updates, sampler trace setup, persistent
  output allocation.
- If warmup mutates persistent trace inputs (token, position, RoPE index, page table, KV cache), reset them to the
  intended capture state just before `begin_trace_capture`.
- A miss that persists: reproduce on a fresh setup with no other live traces, re-running the exact warm forward
  immediately before `begin_trace_capture`. Some ops' signatures depend on transient device state such as free L1
  (see the retained-output entry above); stabilize that signature rather than warming and recapturing repeatedly.

## Generator split

Trace the high-level generator method only if it is already proven trace-safe. The usual split:

- `prepare_decode_inputs_host(...)`: host tensors for token ids, positions, RoPE indices or tables, page tables,
  masks and other per-token inputs.
- `copy_host_to_device(...)` or `ttnn.copy_host_to_device_tensor(...)`: refreshes the stable trace inputs.
- `decode_forward_from_ttnn_inputs(...)` or `ttnn_decode_forward(...)`: the device-only call used for warmup and
  capture.
- `decode_next_token_traced(...)`: refreshes inputs, executes the trace, reads back only what the caller needs.

Device-owned state advances inside the captured graph: `ttnn.plus_one` for current position and RoPE index, the
sampler writing the next token into the decode input through `tt_out_tok`. Vocab-sharded greedy sampling and the
TopK width traps: [performance advice](perf-advice.md#lm-head-and-sampling).

## What stays outside capture

- `ttnn.from_torch(..., device=...)`, `ttnn.as_tensor(..., device=...)`, `ttnn.to_device`,
  `ttnn.copy_host_to_device_tensor`.
- `ttnn.to_torch`, `.cpu()`, `ttnn.get_device_tensors` followed by host conversion, full-logits host composition.
- `ttnn.synchronize_device`, event waits and explicit reads (traces cannot capture events).
- Lazy weight loading, model-cache loads, first-use module initialization.
- Resetting KV cache, page tables, semaphores or sampling state.
- Python decisions that change the op sequence, shape, memory config or code path: decide before capture and bind
  the mode at construction.

## Multi-chip traces

Mesh traces can include collectives. Set the fabric config before opening the mesh, create CCL semaphore managers
before capture, warm with the same mesh shape, page-table shape and CCL topology, make the first allocation of
any persistent CCL output happen before capture (a warm pass on the same path usually does it), and reuse the
same replicated or sharded input allocations for capture and replay. If one decoder layer traces but the full
model does not, inspect the terminal work separately: final distributed norm, hidden gathers, LM head, logits
gather, sampling, argmax and token readback.

## Allocation tracker mechanics

`TT_METAL_TRACE_ALLOC_TRACKING=1` is read once at startup, so set it before TTNN is imported (for serving, in the
server and worker processes). It validates live unsafe allocations before each `ttnn.execute_trace` and raises
`RuntimeError` on survivors; nothing is checked after the last replay, so a workload that compiles after capture
and ends without a replay passes silently. `TT_METAL_TRACE_ALLOC_TRACEBACKS=1` and
`TT_METAL_TRACE_ALLOC_REFERRER_DEPTH` help locate an allocation. Tracked runs are slow because the tracker collects garbage before
every replay; in one bringup a tracked batch-32 decode test took about 500 s, past the repo's default per-test
timeout.

## Debugging trace failures

Fatal signatures: `Writes are not supported during trace capture`, `Reads are not supported during trace capture`,
`Event Synchronization is not supported during trace capture`; replay returns stale outputs or ignores updated
inputs; replay passes once and fails on repetition.

Bisect with flushed markers around coarse blocks:

```python
print("BEGIN_TRACE", flush=True)
trace_id = ttnn.begin_trace_capture(mesh_device, cq_id=0)
print("TRACE_MODEL_START", flush=True)
out = model.decode_forward_from_ttnn_inputs(...)
print("TRACE_MODEL_DONE", flush=True)
ttnn.end_trace_capture(mesh_device, trace_id, cq_id=0)
```

- A write: look for host input creation, lazy weights, cache loads, page-table refresh, sampling-state updates,
  semaphore resets, first-use persistent CCL buffers, or `copy_host_to_device_tensor` hidden in a helper. Or a
  program-cache miss: `device.set_program_cache_misses_allowed(False)` around capture makes a miss fail as
  `"<Op>: program cache miss occurred, but cache misses are forbidden"`, naming the op.
- Stale inputs: compare the tensors the model captured with the tensors refreshed before `execute_trace`;
  `tt_transformers` binds model-side trace inputs before capture and refreshes only those buffers.
- A traced loop slower than the decoder-stack lower bound is usually host-stepped: token, position or RoPE
  refreshes, page-table copies, mask rebuilds, cache resets, synchronizations, blocking replays or feedback
  readbacks in the steady state. A counter such as `position_refreshes = gen_len - 1` shows it.

## Symptoms and their usual mechanism

These are mechanism signatures, not model properties; run the focused experiment before forming a precision or
model-quality theory.

| Symptom | Likely mechanism | Focused experiment |
|---|---|---|
| Every output token emitted twice (or k times) while the text still advances | Decode consumes a stale token or position input: feedback lags replay by one step, often because async scheduler overlap built step N+1 before sampled token N updated host request state | Two-step replay with different tokens and positions; assert the exact tensors the trace reads were refreshed and the outputs differ. In vLLM, repeat with async overlap on and off |
| Greedy output nondeterministic across runs, or wrong after a sampled request | Trace cache keyed too coarsely: sampling mode or params are not in the key, so replay reuses another mode's graph | Alternate greedy and sampled requests back to back; log which trace id each replay uses |
| Wrong output at exactly the capture position, correct afterwards | Capture recorded the cache update but never executed it | Execute the trace once right after capture, then validate the capture-position cache entry |
| One device or replica diverges after layer N while single-chip is clean | Collective-variant divergence on that axis (numerics or ordering of the reduce path) | Compare per-device outputs at layer boundaries; swap the collective variant for the failing axis |
| Serving output repetitive or garbled while the standalone generator is clean | Stale async-decode state across requests: outputs read before refresh, or per-request state not reset | Serve the same prompt twice with another between; diff against the single-request generator, then a multi-request smoke test |
