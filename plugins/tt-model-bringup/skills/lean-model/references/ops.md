# Ops and kernels

## The ladder

For each item at the top of the profile (time lost against its floor), under `release` try in order and stop at
the first that reaches the target. Under `open` all four are on the table from the start: pick by expected gain
against cost from the roofline; a change to the op is often cheaper than a workaround that duplicates it.

1. **Configuration**: program config, core grid, sharding, layout, dtype, fidelity, trace.
2. **Another existing op or variant**: a different ttnn op, a fused variant already in ttnn, or a model-local
   kernel another model already uses.
3. **A model-local fusion**: a `ttnn.generic_op` kernel under the model directory for a chain of small ops,
   intermediates round-tripping DRAM, or gaps inside a traced step. Gain = the chain's measured time minus the
   fused floor.
4. **Changing tt-metal code outside the model** (an op's C++ or kernels, CCL setup, allocator or L1 defaults,
   trace and dispatch, fabric configuration) or **a new ttnn op**: only under `scope` `open` (`model-bringup`).
   Under `release`, steps 1-3 are the whole ladder.

Making an op work is a different question from making it fast: when an op is wrong or rejects the model's
shapes, adapt the inputs to its contract or work around it inside the model under `release`, or fix the op with
a test under `open`; keep a minimal repro either way.

Skip ahead when the roofline and a microbenchmark of the existing op at model shapes show it cannot reach the
target's order of magnitude: start the new op or fusion early, on a side branch, while the plain path keeps the
model correct.

Debugging is not limited by scope: to find a cause, instrument or patch anything on a side branch or worktree
and revert it before the gate. Under `release`, a patch that proved the cause becomes the repro for the issue.

Under `release`, when the profile already shows a large gap that only an op change would close, a quick
measurement of it is worth having: one microbenchmark of a patched op at model shapes on a side branch, no
integration or polish, then a line in the report (op, change, measured gain) for tt-metal maintainers. Skip it
when the gain is small or the measurement would take more than an hour or so.

## Every new or changed path

- Trace-capturable; no host work per step.
- A unit test at model shapes against the plain path or the reference.
- A switch back to the plain path, kept until T2 has passed with the new one.
- A row in `model.json` `ops`: name, kind (fusion, op change, new op), test, switch, measured gain.

## Changing tt-metal code (scope `open`)

The model may keep its own version of the op or code; it does not go back to main. Run the op's existing tests where they
still apply, and note in `model.json` `ops` that the model depends on a changed op. Only one agent builds the C++
tree at a time; the coordinator schedules rebuilds.

## Check kernels against main

Kernel APIs move. Before writing a kernel, read a current example on main that does something similar.

The catalogue of what was tried before, with its gates, is in [performance advice](../../ttnn-knowledge/topics/perf-advice.md),
[matmul and precision](../../ttnn-knowledge/topics/matmul-precision.md) and [fusion](../../ttnn-knowledge/topics/fusion.md).
