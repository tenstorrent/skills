---
name: optimization-loop
description: Optimise tt-train training step time on one chip by repeating measure (MFU, Tracy), attribute, classify every op into useful math / layout / glue / wasted / dispatch, cost with training multiplicity, fix the top item by class, verify with the PCC harness, MFU rerun and Tracy diff, until the plan's roofline target is met. Use as the third phase of train-bringup.
---

# Optimisation loop

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup).
Inputs: `doc/plan.md` (roofline target, freeze map, kernel inventory, memory budget) and
`doc/perf_analysis.md` (baseline MFU and ranked cost table) from the composite phase.

## Hard constraints

Copy these from `doc/plan.md` and the user's instructions into `doc/optimization/README.md` before
iteration 1; the stage review checks every iteration against them.

- Precision: the plan's fidelity (HiFi4 with fp32 accumulation unless the plan says otherwise). A
  lower fidelity is not a candidate and is never swept or "evaluated"; it is not a rejection to record.
- Result metric: the full-model MFU run (TP over the box when the user authorised it). Subset-depth
  profiles and single-chip subset MFU runs rank the next item and are never reported as results.
- One change per commit, reviewed and verified on its own, so the history bisects.
- Any other constraint the user states (no pushes, cool-down protocol, forbidden runs).

## The loop

Repeat until the roofline target is met or every remaining item is a math kernel proven near its
own roofline (see [Exit and gate](#exit-and-gate)). The loop is the work; a stage review is a
checkpoint inside it, not its end. Never end the turn after a review while the target is unmet
and a candidate with a number remains: fix the findings, commit, and start the next iteration.

1. Measure. An MFU run without the profiler gives ms/step and MFU (plan's FLOP count). Tracy runs
   at four doubling depths of whole blocks (real shapes, warm-up steps excluded, op-support count
   sized for the op count) give per-op device time, input shapes with padding, and core count;
   fit per-block and fixed cost and project to the full depth as in the
   [measurement protocol](#measurement-protocol).
2. Attribute. Walk the op sequence of one step (forward, recompute, backward, optimiser) and map
   every op to the line of model code that emitted it.
3. Classify every op into one of the five classes below.
4. Cost every item: microseconds per op × ops per layer × layers × multiplicity, where multiplicity
   counts forward, recompute and the backward mirror. Group ops into chains between two real ops and
   cost the chain, not the op. A chain of individually modest plumbing ops is often the largest item.
5. Pick the top item by class first, then by cost. An overhead item that can go to zero outranks a
   larger math kernel that can only get somewhat faster. However, usually kernel work is the best
   choice (fuse an op into a neighbour's kernel, extend a kernel we own, replace a
   library kernel with one that fits the shape, remove a kernel by changing the contract).
6. Apply the class action using the ladder of fixes. "Reuse" (a library kernel as-is) is the cheapest
   rung, not the best one: a library matmul, attention or norm at well under its roofline on the
   model's shapes is an "extend" or "write new" item, and the loop's larger gains usually come from
   there.
7. Boost a kernel, not only the glue around it. Removing small ops returns a fraction of a percent
   per iteration; once the overhead classes are below the step's noise, each iteration takes one of
   the largest kernels and rewrites its data flow against its own roofline: bytes it moves versus the
   bytes it needs (re-reads of an operand per work item, partials written and read back), cores it
   uses versus the grid, balance of its work items, and DEST / L1 residency. State the kernel's
   roofline (bandwidth or FLOP) and its measured fraction before starting, pick the kernel with the
   largest absolute headroom that the team owns, and target the roofline, not a few percent. A
   library kernel at the top of the table (a matmul at 60-75 % of peak) is a candidate for a
   purpose-built kernel on that shape, after the owned kernels.
7. Verify: PCC harness on the forward and every gradient with relative errors, MFU rerun with the
   baseline command, Tracy diff showing the targeted ops gone and nothing new.
8. Append a row to the MFU history and update the ranked cost table in `doc/perf_analysis.md`
   in the [document format](#document-format). Record rejected candidates with their cost, the
   attempted fix and the reason.
9. Commit the iteration on its own (code, tests, docs, evidence), then continue with the next item.

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

"Inherent" and "at the library's optimum" are claims, not classes. A collective is inherent only
after overlap with compute was tried; a library kernel is at its optimum only after a kernel for the
model's shape was tried or its roofline fraction is stated and above the loop's threshold.

## Measurement protocol

MFU runs: no profiler environment variables, real shapes, real sequence length, the plan's FLOP
count, the full model. When the full model needs more than one chip and the user authorised the box,
the full-model run is the TP run over the box and its MFU is against the box's peak; the result of
every iteration is that number and nothing else. Step-time noise is one to two percent; for smaller
changes report device time, not wall-clock. Never run the watcher and the device profiler together. Use
`tt-device-usage` for resets and hang triage.

Tracy runs never profile the full depth: an LLM's full layer stack under the profiler is too
expensive to collect and to analyse. Instead profile the model at four depths and extrapolate:

1. Define the block. A block is the smallest group of consecutive layers that repeats through the
   model with every layer kind in its real proportion. For a homogeneous decoder the block is one
   layer. For Qwen3-Next the block is four layers (three linear-attention layers then one
   full-attention layer), and two bare layers would not be representative. Record the block in
   `doc/plan.md`.
2. Build the model at four doubling depths of whole blocks: 2, 4, 8 and 16 blocks when the block
   is one layer; 1, 2, 4 and 8 blocks when it is larger. Stop at the largest depth below the real
   block count, so that no run profiles the full model. Keep hidden size, heads, sequence length,
   batch, dtype and recipe at their real values; only the block count changes. Analyse one device
   and divide by the number of profiled steps.
3. Fit. Fit a least-squares line `t(B) = fixed + B × per-block` through the four points. Per-block
   cost is the slope; fixed cost is the intercept (embedding, final norm, lm head, loss, optimiser
   on non-block parameters, per-step dispatch). Do the same fit per op class and per ranked item,
   with ops per block from the attribution.
4. Check the residuals. Every measured depth must sit on the fitted line within a stated tolerance
   (five percent of its own step time is a reasonable default), and the deepest point matters
   most because it is nearest the extrapolation. A point off the line means something in the
   step does not scale with depth (memory pressure, program-cache churn, a host sync that
   appears past some depth); find it and explain it before using the projection. If the
   deviation is real and grows with depth, say so and give the projection as a range, not a
   number.
5. Project. Full-model step time is `fixed + B × per-block` for the real block count `B`; the
   ranked cost table is reported at full depth and labelled as a projection.
6. Validate. Compare the projection with the full-model MFU wall-clock taken without the profiler.
   The difference is the host/dispatch gap plus profiler overhead; a difference beyond the
   expected dispatch gap is a finding, not noise.

Before/after Tracy diffs for a change use the same four depths; a change whose benefit does not
show at every depth is reported with the depth where it stops showing. Record the four raw
measurements, the fit with residuals, the projection and the validation in `doc/perf_analysis.md`.

The depth fit exists to rank the next item. It does not go into the result: once the full model
runs (single chip or the authorised box), each iteration's profile is one subset depth on the
result configuration (e.g. 4 layers at TP over the box, per chip), used for the before/after diff
and the ranking only. No subset-depth MFU run, no projection row and no Tracy table belongs in the
MFU history or the report; they stay in the evidence directory.

## Kernel boost checklist

Before rewriting a kernel, write down in the work log:

- The roofline: bytes the op must move at the measured bandwidth, or FLOPs at the measured clock,
  whichever binds; the measured time and the fraction.
- Where the bytes go: operands read more than once per call (K/V per query row, Q/dO per K/V row,
  the activation per LoRA product), intermediates written and read back (partials, transposes,
  concatenated copies), padding moved for nothing.
- Where the cores go: work items per core and their spread, cores idle, the longest item.
- The structural fix: multicast of a shared operand within a core group, several rows per item with
  the state kept in L1, merged splits with a reduce, a different work-item shape, a fused epilogue.
- The expected time after the fix and the standalone measurement that will confirm it before the
  full-model run.

A boost that lands below half the expected gain is reported with the reason (compute-bound after
all, NoC-bound, imbalance), and the next structural fix, not as a win.

## Document format

`doc/perf_analysis.md`, the README and the work log are read by skimming. Header, sub-headers,
short bullets; no paragraph longer than three lines; numbers in tables. Fixed layout:

1. `# Performance analysis` with one line of setup (model, recipe, metric, denominator).
2. `## MFU history`: one table, five columns (iteration, change, ms/step, MFU, evidence). A row is one
   line; a cell holds a number or a name, never a sentence.
3. `## Ranked cost table`: the current table, class per row, cost at full depth.
4. `## Iteration N` per iteration, exactly these bullets: **Change**, **Result** (the full-model
   number and the per-chip cycle-time delta), **Attribution** (the rows that moved, as numbers),
   **Verification** (harness, gradient check, loss match, with pass/fail), **Evidence** (paths).
   Anything longer than a bullet goes into a file under `evidence/` and is linked.
5. `## Rejected` table (candidate, cost, attempt, reason) and `## Ledger` (anomalies, one bullet each).

A review finding is fixed by correcting the bullet or table cell, not by adding a caveat paragraph.

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
roofline with a documented blocker for the rest. "Near its roofline" needs, per item: the roofline
figure (bytes or FLOPs at the measured clock), the measured fraction, and the kernel attempt that
failed to close the gap (a sweep of a library kernel's program configs is tuning, not an attempt).
Collectives count as a remaining item until overlap with compute was tried. Without these the loop
is not closed; the next iteration starts. `doc/perf_analysis.md` then holds the MFU history,
the final ranked table, the gap decomposition (step time = roofline + math inefficiency + overhead
+ dispatch), memory peak, and every rejected candidate with evidence. PCC harness and loss match
still pass on the final path. `doc/optimization/README.md` and `work_log.md` record commands,
commits, before/after per iteration and artefacts. `stage-review` returns `clean-pass`.
