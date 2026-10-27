# Missing ops

An op is missing when the port needs a computation that no ttnn op or tt_dit layer covers at
the needed shape, layout, or precision. Try these in order and stop at the first that passes
the stage 2 single-op bar on device.

1. **It may not be missing.** Run `tt-dit-kernel-research` first: search for the op under other
   names, check `tt-dit-benchmark-profile/existing-fast-paths.md`, and check whether an
   existing op has the needed parameter but does not expose it to Python.
2. **Composite of existing ttnn ops.** Usually the right first answer: correct, easy to review,
   and a clear target for fusion later. Keep it in the model's module unless other models need
   it.
3. **tt-lang kernel.** For a fused or custom computation that a composite cannot express or
   that stage 6 shows is a tall nail. tt-lang is a Python kernel language for Tenstorrent
   hardware with a functional simulator. It is under active development, so check its current
   README and feature matrix for install steps and supported features before choosing it.
   Develop in the simulator if useful, but the gate is an on-device test.
4. **New or changed ttnn kernel.** Follow `tt-dit-kernel-research` "When a new kernel is
   actually the answer".

Host fallback (torch on the host) is allowed only as a stage 1 placeholder. One left in at the
end of stage 3 needs the user's approval and a line under "Needs review".

For every op added:

- An on-device test against a torch implementation at the production shapes that use it.
- The op name, approach, and location in the stage 3 summary.
- If it changes tt-metal outside the model directory, keep it in its own commit so it can be
  reviewed and upstreamed separately.
