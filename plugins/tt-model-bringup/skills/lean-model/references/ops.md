# Ops and kernels

## Options

The options for an item at the top of the profile, roughly from cheapest to most work. Which to try, and in what
order, is your call.

- **Configuration**: program config, core grid, sharding, layout, dtype, fidelity, trace.
- **Another existing op or variant**: a different ttnn op, a fused variant already in ttnn, or a model-local
  kernel another model already uses.
- **A model-local fusion**: a `ttnn.generic_op` kernel under the model directory for a chain of small ops,
  intermediates round-tripping DRAM, or gaps inside a traced step. Gain = the chain's measured time minus the
  fused floor.
- **Changing tt-metal code outside the model** (an op's C++ or kernels, CCL setup, allocator or L1 defaults,
  trace and dispatch, fabric configuration) or **a new ttnn op**: only under `scope` `open` (`model-bringup`).
  Under `contained`, the first three are the options.

Making an op work is a different question from making it fast: when an op is wrong or rejects the model's
shapes, adapt the inputs to its contract or work around it inside the model under `contained`, or fix the op with
a test under `open`; keep a minimal repro either way.

Debugging is not limited by scope: to find a cause, instrument or patch anything on a side branch or worktree
and revert it before the gate. Under `contained`, a patch that proved the cause becomes the repro for the issue.

Under `contained`, if an op change looks worth a lot, a brief measurement on a side branch and a line in the report
help tt-metal maintainers. Optional; keep it short.

## Every new or changed path

- Trace-capturable; no host work per step.
- A unit test at model shapes against the plain path or the reference.
- A switch back to the plain path, kept until T2 has passed with the new one.
- A row in `model.json` `ops`: name, kind (fusion, op change, new op), test, switch, measured gain.

## Changing tt-metal code (scope `open`)

The model may keep its own version of the op or code; it does not go back to main. Run the op's existing tests where they
still apply, and note in `model.json` `ops` that the model depends on a changed op. Concurrent rebuilds of one C++
tree conflict.

## Check kernels against main

Kernel APIs move. Before writing a kernel, read a current example on main that does something similar.

The catalogue of what was tried before, with its gates, is in [performance advice](../../ttnn-knowledge/topics/perf-advice.md),
[matmul and precision](../../ttnn-knowledge/topics/matmul-precision.md) and [fusion](../../ttnn-knowledge/topics/fusion.md).
