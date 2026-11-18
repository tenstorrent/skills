---
"tt-train-bringup": minor
---

Separate the plan's performance target from the plan's own cost estimate. The roofline section now
produces two numbers: the MFU the user requires (copied from the hard-constraints block, never
derived) and the op-by-op sum, labelled as the cost estimate of the first implementation. A target
summed from the agent's own planned ops is self-fulfilling, and the loop built on it exits at the
baseline: the reference bring-up wrote its 1914 ms/step estimate (15.6 % MFU) into the target field,
measured 15.9 % in iteration 1 and closed the optimisation phase with a clean review. The loop now
reads the target from the hard-constraints block, never lowers it, never re-derives it from a
measurement, and treats a clean-pass review below the target as the end of an iteration rather than
of the phase. `stage-review` fails a plan that carries one number where two belong and a loop closed
below the target.
