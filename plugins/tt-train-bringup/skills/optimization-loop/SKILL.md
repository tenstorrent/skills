---
name: optimization-loop
description: Optimise tt-train training step time on the plan's chips by repeating measure (cooled MFU with telemetry, Tracy), attribute (ops to model code, the top ops to their compiled kernels), classify every op into useful math / layout / glue / wasted / dispatch, cost in wall ms/step with training multiplicity, take the biggest math kernel first, verify with the PCC harness, MFU rerun and Tracy diff, until the plan's roofline target or its stated hardware ceiling is met. Use as the third phase of train-bringup.
---

# Optimisation loop

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup).
Inputs: `doc/plan.md` (roofline target, freeze map, kernel inventory, memory budget) and
`doc/perf_analysis.md` (baseline MFU and ranked cost table) from the composite phase.

## Hard constraints

Copy these from `doc/plan.md` and the user's instructions into `doc/optimization/README.md` before
iteration 1, and read them before every benchmark; the stage review checks every iteration against
them.

- Precision: the plan's fidelity (HiFi4 with fp32 accumulation unless the plan says otherwise). A
  lower fidelity is not a candidate and is never swept, benchmarked "for comparison" or recorded as
  a rejection; a clock or power experiment uses the recipe's fidelity too.
- Result metric: cooled wall ms/step of the full model on the plan's chips (TP over the box when the
  model needs it) and MFU against the nominal peak (the plan's figure, e.g. 1350 MHz). "MFU at the
  delivered clock" is a diagnostic only, never a result row. Subset-depth profiles and
  single-chip subset MFU runs rank the next item and are never reported as results.
- Batch and sequence length as the recipe states them.
- One change per commit, reviewed and verified on its own, so the history bisects.
- Any other constraint the user states (no pushes, cool-down protocol, forbidden runs).

## The loop

Repeat until the roofline target is met, the plan's hardware ceiling is reached, or every remaining
item is a math kernel proven near its own roofline (see [Exit and gate](#exit-and-gate)). The loop
is the work; a stage review is a checkpoint inside it, not its end. Never end the turn after a
review while the target is unmet and a candidate with a number remains: fix the findings, commit,
and start the next iteration.

Begin kernel optimisation after the composite phase passes its
[first-baseline checks](../composite-bringup/SKILL.md#before-accepting-the-first-baseline).
Use the plan's useful-FLOP count and actual-chip nominal denominator, keeping recompute overhead
out of the useful numerator.

Iteration 1 is the biggest math kernel. The matmuls are most of a transformer step (63 % in the
reference bring-up, where they stayed untouched for 34 iterations while glue and small owned
kernels returned a few percent each). Before any glue work, attribute the top ttnn op to its kernel
(compiled source and any fork, DEST sync mode, cycles per tile against the FPU peak, bytes against
bandwidth) and make an own kernel for the model's shapes, or a ttml fork of its program factory,
the first rung. Glue, layout and small owned ops come in only when deleting them removes a whole op
chain whose measured cost beats the kernel's expected gain.

1. Measure. A cooled MFU run without the profiler, telemetry sampler running, gives wall ms/step,
   MFU at the nominal peak, the per-chip settled clock and the critical chip. Tracy runs at four
   doubling depths of whole blocks (real shapes, warm-up steps excluded, op-support count sized for
   the op count) give per-op device cycles, input shapes with padding, and core count; fit per-block
   and fixed cost and project to the full depth as in the
   [measurement protocol](#measurement-protocol).
2. Attribute. Walk the op sequence of one step (forward, recompute, backward, optimiser) and map
   every op to the line of model code that emitted it. For the top three ops also map to the kernel:
   which program factory and kernel files compile, the sync mode, the core grid, the % of peak.
3. Classify every op into one of the five classes below.
4. Cost every item in wall ms/step at full depth: microseconds per op × ops per layer × layers ×
   multiplicity, where multiplicity counts forward, recompute and the backward mirror, with Tracy's
   cycles converted at the delivered clock. Group ops into chains between two real ops and cost the
   chain, not the op. A chain of individually modest plumbing ops is often the largest item.
5. Pick the biggest item. Rank by ms/step. The top of the table is normally a library kernel; its
   own-kernel attempt is this iteration, not a later one. An overhead item outranks it only when its
   deletion removes a whole chain with a measured cost above the kernel's expected gain. Order
   kernel rungs by expected ms, largest first: never spend a day on knob sweeps or ports of a small
   kernel while a larger kernel is untried (the reference bring-up lost a day on four such rungs
   for 0 ms).
6. Apply the class action using the ladder of fixes. "Reuse" (a library kernel as-is) is the cheapest
   rung, not the best one: a library matmul, attention or norm at well under its roofline on the
   model's shapes is an "extend" or "write new" item, and the loop's larger gains come from there.
7. Boost a kernel, not only the glue around it. Removing small ops returns a fraction of a percent
   per iteration; each iteration takes one of the largest kernels and rewrites its data flow against
   its own roofline: bytes it moves versus the bytes it needs (re-reads of an operand per work item,
   partials written and read back), cores it uses versus the grid, balance of its work items, and
   DEST / L1 residency. State the kernel's roofline (bandwidth or FLOP) and its measured fraction
   before starting, pick the kernel with the largest absolute headroom, and target the roofline, not
   a few percent. A library kernel at the top of the table (a matmul at 60-75 % of peak) is the
   first candidate for a purpose-built kernel on that shape, before the owned kernels that are a few
   percent of the step each.
8. Verify: PCC harness on the forward and every gradient with relative errors (at the shard shapes
   on a multi-chip layout), loss match at the plan's layout, MFU rerun with the baseline command,
   Tracy diff showing the targeted ops gone and nothing new. For a change that is not bit-exact, the
   gate is the **nearest** control, not the most authoritative one: the previous iteration's own
   trajectory at the same seed isolates this one commit in seconds, while a torch reference measures
   every difference between the two stacks at once and its drift term belongs to whichever of them
   compounds. One such gate ran a 24B torch reference on CPU for 92 minutes, held all four chips and
   answered the wrong question; the committed device-against-device comparison settled it with no
   device time at all.
9. Append a row to the result table and update the ranked cost table in the
   [document format](#document-format) and [report style](#report-style): what changed and by
   how much, in words and numbers, not a path. Record rejected candidates with their cost, the
   attempted fix, the reason and the condition under which to retry them.
10. Commit the iteration on its own (code, tests, docs, evidence), then continue with the next item.
    A stage review runs every eight iterations or at a milestone (a kernel landed, a target
    crossed, a close claimed), not after every iteration.

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
step time minus the sum of device kernel durations (converted to the delivered clock; a gap that
equals the clock ratio is the power cap, not dispatch). Action: fewer ops (every overhead op removed also
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

Adapted retry. A rejected item is retried when the layout around it changes (a neighbouring op
fused, a shard shape or a kernel replaced, a contract changed): the rejected table carries a
"retry when" column and the loop checks it every iteration, not 40 iterations later (the reference
bring-up rejected a fusion at iteration 7 and landed it at 48 under a changed layout).

Collectives. A sub-device overlap attempt is made only with persistent buffers and a TP loss match
in the same chain: an overlap that corrupts a buffer shows as loss drift, not as a crash, and a
silent loss script hides it. If the overlap fails, record the floor and reduce the bytes instead
(sequence parallel, fewer gathers), not a second overlap attempt.

Never stop to ask for scope. When the owned rungs are done the next rung is a kernel inside ttnn,
a sharding change or a recipe change inside the hard constraints; propose it with numbers in the
work log and do it. "Try both" means both candidates are built and measured. The user's stated
target and the hard constraints are the scope; a question to the user is for a hardware decision
(a power limit, a board) or a constraint change, nothing else.

## Keep the big rows first

The loop's failure mode is not a wrong measurement, it is a run that spends its second day pricing
3-12 ms rows while the two largest rows in the table sit closed on narrow evidence. Guard against it
explicitly:

- **A pre-check falsifies one mechanism, never a row.** "Work balance returns 0" and "the scalar
  bytes return 7 %" say nothing about whether the kernel's reader can be restructured. A row is
  closed only after a data-flow attempt on it: which operand is re-read per work item, what is
  multicast within a core group, how many rows a work item keeps in L1, where the partials go. Until
  then the row stays open and names the mechanisms still unpriced.
- **The biggest row gets the next iteration.** An overhead or configuration item jumps the queue
  only with the chain-deletion reason: its measured cost above the big kernel's expected gain. Three
  consecutive iterations without a ΔMFU means the ranking is being avoided; take the top row and
  rewrite its data flow.
- **Never derive a hardware ceiling from your own rejection list.** Summing the ceilings of the
  things you decided not to build and calling the total the achievable end state is circular: the
  total is a statement about what you chose to price, not about the hardware. A ceiling claim needs
  a roofline figure and a failed kernel attempt per remaining row, and it does not end the loop on
  its own.
- **A well-optimised training step reaches roughly 40-50 % of the nominal peak. Keep going until
  you are there.** A run sitting in the low thirties has unexplored kernel work, not a ceiling, and
  the table usually says where: base matmuls well under the delivered FLOP rate, attention still on
  the library's data flow, norms and elementwise chains still on their own rows rather than folded
  into a neighbour or onto a shard. Treat a plateau as a signal to go after a bigger row, not as
  evidence that the board is finished. State the delivered-clock margin as the margin it is, and do
  not let the power cap stand in for work you have not attempted. There is a way; find it.

## Measurement protocol

MFU runs: no profiler environment variables, real shapes, real sequence length, the plan's FLOP
count, the full model. When the full model needs more than one chip and the user authorised the box,
the full-model run is the TP run over the box and its MFU is against the box's nominal peak; the
result of every iteration is that number and nothing else. Never run the watcher and the device
profiler together. Use `tt-device-usage` for resets, telemetry and hang triage.

Measurement rules, all in one place:

- Every MFU run is cooled by the same protocol (a stated idle time before the run, the same for
  every row) and carries the telemetry sampler; the result row states the per-chip settled clock,
  TDP and the critical chip. A run whose clock differs from the previous row's by more than the
  noise is rerun before it is compared.
- A/B comparisons run ABAB inside one process and one script. Standalone numbers from different
  processes are not comparable (the reference bring-up saw 15-20 % clock drift between processes
  on the same kernel); a before/after of a kernel is one script that runs A, B, A, B and reports
  the medians.
- Tracy's durations are cycles at the nominal clock, not wall time. Under a power cap, wall time is
  longer by nominal/delivered; convert before summing device time or computing the host gap, and
  never mix the two in one table.
- Decide between kernel variants by cooled wall ms with telemetry, not by cycles: under a TDP cap a
  variant that moves fewer bytes or spins fewer cores can raise the clock, and its wall gain
  exceeds its cycle gain (and the reverse).
- Step-time noise is one to two percent; for smaller changes report device time with the clock
  stated.
- No forbidden fidelity, batch or run type, in any run, including clock experiments.

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
measurements, the fit with residuals, the projection and the validation in the evidence directory,
linked from the measurement-traps section.

The depth fit exists to rank the next item. It does not go into the result: once the full model
runs (single chip or the authorised box), each iteration's profile is one subset depth on the
result configuration (e.g. 4 layers at TP over the box, per chip), used for the before/after diff
and the ranking only. No subset-depth MFU run, no projection row and no Tracy table belongs in the
result table or the report; they stay in the evidence directory.

## Kernel boost checklist

Before rewriting a kernel, write down in the work log:

- The roofline: bytes the op must move at the measured bandwidth, or FLOPs at the measured clock,
  whichever binds; the measured time and the fraction.
- Where the bytes go: operands read more than once per call (K/V per query row, Q/dO per K/V row,
  the activation per LoRA product), intermediates written and read back (partials, transposes,
  concatenated copies), padding moved for nothing.
- Where the cores go: work items per core and their spread, cores idle, the longest item.
- Under a power cap: the bytes and active cores the variant removes, since those return clock on
  every chip; the expected wall gain, not only the cycle gain.
- The structural fix: multicast of a shared operand within a core group, several rows per item with
  the state kept in L1, merged splits with a reduce, a different work-item shape, a fused epilogue.
- The expected time after the fix and the standalone measurement (ABAB in one process) that will
  confirm it before the full-model run.

A boost that lands below half the expected gain is reported with the reason (compute-bound after
all, NoC-bound, imbalance), and the next structural fix, not as a win.

## Document format

`doc/optimization/README.md`, `doc/perf_analysis.md` and `work_log.md` are read by skimming and
updated once per iteration, three places and no more (the reference bring-up kept four, with
placeholder tokens, and spent 19 commits on their hygiene). Numbers in tables; no placeholder or
`*_PENDING` tokens (a row is written when its number exists). Fixed layout:

1. `doc/optimization/README.md`: the hard-constraints block, then `## Results`: one table, columns
   iteration, **tag**, what changed, ms/step, ΔMFU (points against the previous row), MFU,
   critical-chip clock, **time since start**, verified (yes/no). Nothing else in the file.
2. `doc/perf_analysis.md`: `# Performance analysis` with the setup as bullets (model, recipe,
   metric, denominator, target); `## Results` (the same table); `## Where the step time goes` (op or chain, class, ms/step, % of step, kernel name,
   status: open / landed N / rejected N); `## Tried and rejected` (candidate, cost, what was tried,
   why it failed, retry when); `## Measurement traps hit in this run` (one bullet each). Tables are
   replaced each iteration, not appended. Prose is a bullet or it is deleted; a section of
   paragraphs is a defect.

   **Tag** is what kind of change it was, so a reader can see at a glance where a run's wins come
   from and a later run can compare. Use the narrowest that fits: `new kernel`, `mod kernel`,
   `rm overhead op`, `layout change`, `lib config` (a library program config only, no kernel source
   changed), `collective`, `run script mod`, `repricing` (table corrected, no code), `priced only`
   (ceiling measured, not built), `gate` (correctness only), `rejected`, `misc`. A run whose tags
   are mostly `lib config`, `repricing` and `rejected` is a run that has stopped optimising.

   **Time since start** is hours from the plan commit, on every row including the rejected ones. It
   is the only column that makes two bring-ups comparable, and it is the column that shows a
   plateau: if the last row with a ΔMFU is twenty hours behind the newest row, the loop is stuck
   and the next iteration must be the biggest row in the table, not another pre-check.

   **"What changed" is a sentence a reader understands without the commit.** Name the thing, what it
   does now, and what it did before; for a rejection, say what was tried and why it failed, in the
   same cell. "RMSNorm core count alone — rejected at pre-check" and "RMSNorm reads gamma's one real
   row, not the padded tile" are both too short to be useful. Write "give RMSNorm more cores without
   changing its work item — rejected at pre-check: every core re-read the whole gamma, so traffic
   scaled with the grid" and "RMSNorm now reads gamma as its one real row instead of the full padded
   tile".

   **No private vocabulary.** Section and column names are what an engineer who has not read this
   skill would call them. "Ledger", "rung", "arm A" and similar in-house words do not go in the
   model's documents; say "measurement traps", "the cheapest fix that works", "the baseline arm".
3. `work_log.md`: one bullet per iteration, three lines at most:
   `N: <what changed and how> | <ms/step, ΔMFU> | harness <pass/fail>, loss <pass/fail> | <sha>`.
   A path only when there is a figure or table worth opening.
4. `evidence/`: compact files only (gzip CSV, PNG at 70 dpi, compact JSON), every file under 500 KB.
   Tracy dumps and raw profiler directories stay outside the repository tree; the evidence directory
   holds the per-op summary CSV and the command that produced it.

A review finding is fixed by correcting the bullet or table cell, not by adding a caveat paragraph.

### Report style

Every row and bullet answers three questions and nothing else: what changed (the mechanism, in
words a kernel engineer reads once), how much (ms/step and ΔMFU in points, with the clock when it
moved), and whether it is in (verified, landed) or out (rejected, why). Important stays, the rest
goes: no method restated, no hedging, no narrative of the attempts that produced no number, no
link in place of the content, no caveat paragraphs. Short full words; exact op, kernel and knob
names; no invented abbreviations.

| bad | good |
|---|---|
| Improved the attention backward kernel, see `evidence/iter17_sdpa_bw.md` for details and analysis | sdpa_bw: K/V multicast within core group, 2 query rows per work item |
| Investigated the gating path and applied a fusion that removes several ops | gate mul fused into rmsnorm epilogue; 3 binary ops gone per layer |
| Tuned the matmul configuration to improve utilisation | gate_up matmul: in0_block_w 4 to 8, 2D mcast grid 8x11 |
| MFU went from 29.4 % to 30.1 % as measured by the full-model run | 436.1 ms, +0.7 pt, 30.1 % |

The same style holds for the composite README, the plan's tables and the review verdicts: a
finding is one line of what is wrong and one line of what to do.

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

Every compute kernel calls the `*_init` of each LLK it uses before its first tile; a skeleton
without it hangs the chip (twice in the reference bring-up). Before a device run, confirm the build
is fresh (the installed binary's timestamp is after the last edit; a stale build hung the chip and
needed `tt-smi -r`) and that a patch applied (the patched op appears or disappears in a short op
list) before measuring: a patch script that ran inside the background chain once measured the
unfused op. Apply, verify, then launch the chain.

## Exit and gate

The loop ends when the measured step time is within the stated margin of the plan's roofline
target, or the plan's hardware ceiling is reached (the delivered-clock margin stated on day 1, with
the telemetry showing the cap in every result row), or every remaining item in the ranked table is
useful math at a stated fraction of its own
roofline with a documented blocker for the rest. A sum of unbuilt ceilings is not a hardware
ceiling, and "the target is unreachable" is not an exit. "Near its roofline" needs, per item: the roofline
figure (bytes or FLOPs at the measured clock), the measured fraction, and the kernel attempt that
failed to close the gap (a sweep of a library kernel's program configs is tuning, not an attempt).
Collectives count as a remaining item until overlap with compute was tried. Without these the loop
is not closed; the next iteration starts. The README then holds the full result table and
`doc/perf_analysis.md` the final ranked table, the gap decomposition (step time = roofline + math inefficiency + overhead
+ dispatch), memory peak, and every rejected candidate with evidence. PCC harness and loss match
still pass on the final path. `doc/optimization/README.md` and `work_log.md` record commands,
commits, before/after per iteration and artefacts. `stage-review` returns `clean-pass`.
