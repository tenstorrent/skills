# Ops and kernels

## The ladder

For each item at the top of the profile (time lost against its floor), try in order and stop at the first that
reaches the target:

1. **Configuration**: program config, core grid, sharding, layout, dtype, fidelity, trace.
2. **Another existing op or variant**: a different ttnn op, a fused variant already in ttnn, or a model-local
   kernel another model already uses.
3. **A model-local fusion**: a `ttnn.generic_op` kernel under the model directory for a chain of small ops,
   intermediates round-tripping DRAM, or gaps inside a traced step. Gain = the chain's measured time minus the
   fused floor.
4. **A change to a ttnn op**: when the op is the problem for every model, not only this one.
5. **A new ttnn op**: when nothing in ttnn does the job at model shapes.

Skip ahead when the roofline shows the existing path cannot reach the target's order of magnitude: start the
new op or fusion early, on a side branch, while the plain path keeps the model correct.

## Every new or changed path

- Trace-capturable; no host work per step.
- A unit test at model shapes against the plain path or the reference.
- A switch back to the plain path, kept until T2 has passed with the new one.
- A row in `model.json` `ops`: name, kind (fusion, op change, new op), test, switch, measured gain.

## Changing a shared ttnn op

Other models use it. Prefer adding an option to changing a default. Run the op's existing tests and find the
other models that call it. Only one agent builds the C++ tree at a time; the coordinator schedules rebuilds.
Write the change so it could go upstream as its own pull request.

## Check kernels against main

Kernel APIs move. Before writing a kernel, read a current example on main that does something similar.
