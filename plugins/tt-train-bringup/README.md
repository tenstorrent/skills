# tt-train-bringup

Bring up and optimise **training** of a Hugging Face model in `tt-train` (ttml) on a single chip, or
on the box the user authorises: the plan costs DP, TP and DP x TP on every allowed mesh shape and
picks the fastest layout that fits. Mesh setup is checked with `scripts/tt_mesh_smoke.py`.

Three phases, run in order by `scripts/multigoal` or one at a time by hand:

1. `train-plan` — hard constraints, kernel inventory (with the top ops' compiled kernels), freeze map,
   roofline target at the nominal peak, memory budget, hardware ceiling (clock ladder, power cap,
   critical chip, multi-chip fit).
2. `composite-bringup` — the full model in ttml autograd on the plan's chips, existing kernels where the
   inventory has them, composed ops elsewhere; first-baseline checks for skipped frozen gradients,
   one-time constant preparation, layouts and actual shard configs; PCC harness on the forward and
   every gradient; loss matches torch. Useful FLOPs follow the recipe (including frozen-base work
   for LoRA), and MFU uses the actual chips' nominal peak.
3. `optimization-loop` — measure (cooled MFU with telemetry + Tracy), attribute, classify every op
   into useful math / layout / glue / wasted / dispatch, cost in wall ms/step with training
   multiplicity, take the biggest math kernel first, verify, repeat until the roofline target or
   the hardware ceiling is met.

Tracy never runs on the full model: profiles are taken at 1x, 2x and 4x the plan's representative
slice (every layer kind in its real proportion, at least two layers) with real shapes, as a per-op
cost table per slice copy with a scaling check across the three sizes. They rank items and diff
changes only; no full-step time is projected from them, and the result is the full-model MFU run
without the profiler (see the
[measurement protocol](skills/optimization-loop/SKILL.md#measurement-protocol)).

Hardware telemetry: `scripts/tt_telemetry_sampler.py` (pyluwen, 20 Hz per-chip clock/power CSV),
`scripts/tt_telemetry_summary.py` (settled clock, critical chip, power cap) and
`scripts/tt_clock_ladder.py` (the day-1 clock ladder for the plan's hardware ceiling).

Entry point: [`skills/train-bringup/SKILL.md`](skills/train-bringup/SKILL.md). Requires
`tt-autodebug` installed and enabled.

Also included, usable on its own without the phases above:

- `tt-train-pr-review` — strict pre-merge review of tt-metal PRs that touch `tt-train/`: the
  tt-train team's kernel perf patterns, recurring bug classes, test/benchmark/CI conventions and a
  multi-agent adversarial review process. Use it to prepare or review a tt-train PR, or to decide
  whether an optimisation is worth doing. See
  [`skills/tt-train-pr-review/SKILL.md`](skills/tt-train-pr-review/SKILL.md).
