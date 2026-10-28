---
"tt-train-bringup": minor
---

Add `tt-train-bringup`: single-chip training bring-up for tt-train in three phases, plan (kernel
inventory, freeze map, roofline target, memory budget), composite bring-up with a PCC harness on the
forward and every gradient, and an optimisation loop that classifies every op as useful math,
layout, glue, wasted or dispatch, costs it with training multiplicity and fixes it until the
roofline target is met. Tracy profiles are taken at four doubling depths of whole blocks (the smallest
repeating group of layer kinds) and projected to the full depth. Also ships `tt-train-pr-review`, a strict pre-merge review skill for tt-metal PRs that
touch `tt-train/`. Requires `tt-autodebug`.
