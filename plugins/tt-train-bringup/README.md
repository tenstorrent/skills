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

Entry point: [`skills/train-bringup/SKILL.md`](skills/train-bringup/SKILL.md). Requires
`tt-autodebug` installed and enabled.
