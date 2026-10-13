---
name: optimization-loop
description: Optimise tt-train training step time on one chip by repeating measure (MFU, Tracy), attribute, classify every op into useful math / layout / glue / wasted / dispatch, cost with training multiplicity, fix the top item by class, verify with the PCC harness, MFU rerun and Tracy diff, until the plan's roofline target is met. Use as the third phase of train-bringup.
---

# Optimisation loop

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup).
Inputs: `doc/plan.md` (roofline target, freeze map, kernel inventory, memory budget) and
`doc/perf_analysis.md` (baseline MFU and ranked cost table) from the composite phase.

## The loop

Repeat until the roofline target is met or every remaining item is a math kernel already near its
own roofline.

1. Measure. An MFU run without the profiler gives ms/step and MFU (plan's FLOP count). A Tracy run
   on a reduced model (one real layer of each kind, warm-up steps excluded, op-support count sized
   for the op count) gives per-op device time, input shapes with padding, and core count.
2. Attribute. Walk the op sequence of one step (forward, recompute, backward, optimiser) and map
   every op to the line of model code that emitted it.
3. Classify every op into one of the five classes below.
4. Cost every item: microseconds per op × ops per layer × layers × multiplicity, where multiplicity
   counts forward, recompute and the backward mirror. Group ops into chains between two real ops and
   cost the chain, not the op. A chain of individually modest plumbing ops is often the largest item.
5. Pick the top item by class first, then by cost. An overhead item that can go to zero outranks a
   larger math kernel that can only get somewhat faster.
6. Apply the class action using the ladder of fixes.
7. Verify: PCC harness on the forward and every gradient with relative errors, MFU rerun with the
   baseline command, Tracy diff showing the targeted ops gone and nothing new.
8. Append a row to the MFU history and update the ranked cost table in `doc/perf_analysis.md`.
   Record rejected candidates with their cost, the attempted fix and the reason.

## Op classes

Every op in the profile falls into one of five classes. The class, not the op's size, determines how
the op shows up, which fix applies and how much time it can return. Step time is the sum of the
classes, but only the first is the model; the rest exists because of how we expressed it.

Useful math is the model's arithmetic: matmuls (forward, dX, dW), attention and its backward,
linear-attention scans, norm reductions, activations, the optimiser update. It shows at the top of
the microsecond ranking by op code, with low core count against the grid, low utilisation in
`tt-perf-report`, or degenerate shapes (a `[1024, 5120] × [5120, 32]` LoRA matmul). Action: use the
full grid, split work to match the tensor shape (two-phase reductions for short-wide or tall-thin
tensors), tiling and program configs, dtype per tensor group, restructure degenerate shapes, kernel
internals. Ceiling: the roofline.

Layout overhead is the same numbers in a different order or padding: reshape, permute, transpose,
concat, slice, untilize, tilize, typecast, repeat-interleave. It shows as shapes whose padded and
logical sizes differ (`1024x32[12]x128` is 12 heads padded to 32), op pairs that undo each other,
and any forward slice or shift whose backward is a concat-with-zeros followed by an add. Action:
make the producer or consumer kernel address the other's layout through its reader or writer,
change the layout contract at the module boundary, or permute weights at load time so a slice is
tile-aligned. Ceiling: zero.

Glue overhead is small elementwise work between two real ops: a gate multiply after a norm, a LoRA
scale and add, residual and gradient-accumulation adds, a typecast after a reduction. It shows as
many binary or unary ops on the same shape between two large ops. Action: fuse into the neighbouring
kernel's prologue or epilogue (the norm gets the gate multiply, the matmul gets the bias or
activation, accumulate in place). Ceiling: zero, but each op is small, so cost it first.

Wasted overhead is work whose result nobody consumes or that recomputes a constant: gradients for
frozen weights, gammas or taps; a frozen embedding table untilized every step; ops inside the
recompute region that need not be there. It shows as a backward op whose output feeds a tensor
with `requires_grad` false, or an identical op and shape every step on a frozen input. Action: skip
through the freeze map, do it once at load time, move it out of the recompute region. Ceiling: zero.

Host and dispatch gap is device idle time between kernels: per-op launch latency, program-cache
misses, host synchronisation, autograd bookkeeping. It is not a row in the profile; it is wall-clock
step time minus the sum of device kernel durations. Action: fewer ops (every overhead op removed also
removes its launch), trace capture of the static part of the step, no host syncs inside the step.
Ceiling: the device time itself.

## Rules

Cost before you build. A candidate without a number from the profile is not a candidate. Training
sets the multiplicity: every forward op has a backward mirror, recompute runs the forward again, and
the mirror is often more expensive than the op itself. In the reference bring-up the gate multiply
after a norm looked like the obvious fusion and was worth 0.5 % of the step; the reshapes around it
were worth 5 %.

Ask why, not how. For an overhead op the question is never how to make it faster but why it is in
the graph. Layout ops exist because a producer and a consumer disagree on where an axis lives; glue
exists because a kernel lacks an epilogue; waste exists because a backward does not know its
parameter is frozen.

Ladder of fixes, cheapest rung first; go down only when the rung above cannot remove the item.
Delete: change the graph or layout so the op is never emitted. Reuse: call an existing ttnn or ttml
kernel as-is (check the kernel inventory). Extend: add a mode, flag or epilogue to a kernel we own.
Write new: reader, compute and writer kernels with factory, device op, binding, autograd function
and tests.

A change must beat the best correct measured path, not the composite baseline. Fewer ops or a
cleaner graph is not evidence; the MFU rerun and Tracy diff are.

## Measurement protocol

MFU runs: no profiler environment variables, real shapes, real sequence length, the plan's FLOP
count. Tracy: reduced model, one real layer of each kind, analyse one device, divide by profiled
steps, scale per-layer costs by layer counts, validate the projection against full-model
wall-clock. Step-time noise is one to two percent; for smaller changes report device time, not
wall-clock. Never run the watcher and the device profiler together; never profile the full stack.
Use `tt-device-usage` for resets and hang triage.

## Adding a kernel

When the ladder reaches "extend" or "write new", copy the skeleton of an existing ttml metal op
(`tt-train/sources/ttml/metal/ops/rmsnorm_fw` or `gated_rmsnorm`): public entry, device operation
(validation, output specs, program hash including every define), program factory (circular buffers,
compile-time and runtime args, tensor accessors), kernels (CB map header, reader, writer, compute),
registration in `CMakeLists.txt`, `metal/operations.hpp` and `nanobind/nb_ops.cpp`, an autograd
`Function` in Python, and a torch-PCC test for the forward and every gradient. Every push and pop on
a circular buffer must be the same size; `matmul_tiles(in0, in1)` puts `in0` in SrcB and `in1` in
SrcA; fp32 DEST holds four tiles per acquire; a compile-time work count needs one compute kernel per
core group. If a failure crosses several ops or kernels and narrowing stalls, use `autofix`.

## Exit and gate

The loop ends when the measured step time is within the stated margin of the plan's roofline
target, or every remaining item in the ranked table is useful math at a stated fraction of its own
roofline with a documented blocker for the rest. `doc/perf_analysis.md` then holds the MFU history,
the final ranked table, the gap decomposition (step time = roofline + math inefficiency + overhead
+ dispatch), memory peak, and every rejected candidate with evidence. PCC harness and loss match
still pass on the final path. `doc/optimization/README.md` and `work_log.md` record commands,
commits, before/after per iteration and artefacts. `stage-review` returns `clean-pass`.
