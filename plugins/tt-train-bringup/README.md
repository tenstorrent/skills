# tt-train-bringup

Bring up and optimise **training** of a Hugging Face model in `tt-train` (ttml) on a single chip.

Three phases, run in order by `scripts/multigoal` or one at a time by hand:

1. `train-plan` — kernel inventory, freeze map, roofline target, memory budget.
2. `composite-bringup` — the full model in ttml autograd on one chip, existing kernels where the
   inventory has them, composed ops elsewhere; PCC harness on the forward and every gradient; loss
   matches torch.
3. `optimization-loop` — measure (MFU + Tracy), attribute, classify every op into useful math /
   layout / glue / wasted / dispatch, cost with training multiplicity, fix the top item by class,
   verify, repeat until the roofline target is met.

Tracy never profiles the full layer stack: profiles are taken at 2, 4 and 8 layers with real shapes,
fitted to a per-layer plus fixed cost, and projected to the full depth (see the
[measurement protocol](skills/optimization-loop/SKILL.md#measurement-protocol)).

Entry point: [`skills/train-bringup/SKILL.md`](skills/train-bringup/SKILL.md). Requires
`tt-autodebug` installed and enabled.

Also included, usable on its own without the phases above:

- `tt-train-pr-review` — strict pre-merge review of tt-metal PRs that touch `tt-train/`: the
  tt-train team's kernel perf patterns, recurring bug classes, test/benchmark/CI conventions and a
  multi-agent adversarial review process. Use it to prepare or review a tt-train PR, or to decide
  whether an optimisation is worth doing. See
  [`skills/tt-train-pr-review/SKILL.md`](skills/tt-train-pr-review/SKILL.md).
