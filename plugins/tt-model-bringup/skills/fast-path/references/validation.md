# Validation and release gates

Every goal writes one evidence file, `models/autoports/<model>/doc/fast_path/<phase>.json`, and
the goal's check script runs `${TT_MODEL_BRINGUP_ROOT}/scripts/check_fast_path.py --phase <phase>`
on it (exit 0 pass, 1 advisory, 2 critical, 3 checker error). The checker enforces the thresholds below; the work log
and README explain them. Accuracy values are percentages (0-100); times are milliseconds.

## Every evidence file

`phase` (the file's phase name), `hf_model`, `hardware`, `commits` (name → SHA, at least one),
and `evidence` (paths to the raw logs and JSON behind every number, relative to the model
directory or the checkout root; every path must exist).

## How to measure

- **Device steps:** traced batch-1 (or batch-k) decode, mean over at least 128 tokens after
  warmup, on a quiet host (load average below 3 at the start; record it). A step measured under
  load is not evidence.
- **Served numbers:** the same harness, workload, profile and image on both sides of a
  comparison. Compare against the previous build in the same session when the difference is
  small.
- **Accuracy:** teacher-forced top-1, top-5 and PCC against the HF reference logits (fp32) at a
  fixed set of at least 512 natural-text positions; mean of two runs when a change is close to a
  bar. Keep the positions fixed for the whole track.
- **Digests:** a hash of the logits at the reference positions (and of greedy text) proves a
  change bit-identical; compare digests, not PCC, for changes that should not move numerics.
- **Quality:** full GSM8K and IFEval on the main profile at the release; GSM8K-250 and IFEval-100
  at concurrency 1 on each fast path (the 32-way default barely exercises a batch-1 path). Chat
  templates that write the date into the system prompt move subset scores by several points from
  day to day: only same-day pairs on identical prompts compare, and a per-question paired test
  (McNemar) decides whether a difference is real.

## Goal 1: roofline

`peak_gbps_per_chip`; `meshes` → per mesh `tp`, `formats`, `gb_per_token`, `gb_per_chip`,
`floor_ms`, `baseline_step_ms`, `baseline_pct_of_peak`, `free_dram_gb_per_device`; `plan` (list of
`{lever, expected}`). Critical: `floor_ms` more than 5% off `gb_per_chip / peak`, or a baseline
step below 98% of its floor (the accounting is wrong). Advisory: `gb_per_chip` more than 10% off
`gb_per_token / tp` without `replicated_roles`, or a % of peak more than 2 points off.

## Goal 2: streaming decode

`mesh`; `device_step_ms.before/after` → `short`, `long` with `context_tokens.short/long`;
`pct_of_peak_after`; `accuracy` → `positions`, `stock` and `fast` (`top1`, `top5`, `pcc`);
`served` → `c1_tpot_ms_before/after`; `kv_blocks.before/after`; `decode_only_gb_per_device`;
`trace` → `allocation_tracker_clean`, `program_cache_growth`; `fallbacks` (list); `switch`.

Critical: the short step not faster, the long step more than 2% slower, fast top-1 more than 0.5
points or top-5 more than 0.3 points below stock, fewer than 256 positions, served TPOT not
faster, fewer KV blocks without `hard_limit_evidence`, a dirty allocation tracker or any
program-cache growth during the workload, no fallbacks or no switch. Advisory: TPOT more than 10%
above the device step (host overhead), fewer than 512 positions.

## Goal 3: mesh variants

`meshes` → per mesh `status` (`fused`, `opt_in`, `stock`, `unsupported`). Fused and opt-in meshes
carry `device_step_ms`, `accuracy`, `kv_blocks`, `weights` (`decode_copies` or `shared_layout`) and
`profile`; opt-in meshes add `tradeoff`. Stock and unsupported meshes carry a `reason`. A mesh
documented in goal 2 may say `"see": "streaming_decode"`. The goal 2 step, accuracy and KV gates
apply per mesh.

## Goal 4: multi-user decode

`mesh`; `moe`; `widths` → per width (including `1`) `before_ms`, `after_ms`, `path`;
`accuracy_by_width` → per width (including `1`) `top1`, `top5`; `ladder` → `{concurrency, output_tok_s_before, output_tok_s_after}`;
`cost_fit` (`fixed_ms`, `per_expert_ms`) when `moe` is true. Critical: any width more than 2%
slower, no width above 1 faster, a fast width more than 0.5/0.3 points below width 1's accuracy,
a ladder row below 97% of before, or a missing cost fit for an MoE model.

## Goal 5: prefill

`ttft_ms` → per mesh, per prompt length `{before, after}`; `bit_identical` with `digests.before`
and `.after` (or, when false, an `accuracy` block passing the goal 2 bars); `deterministic` →
`status` (`opt_in`, `default`, `not_needed`, `not_implemented`), `runs`, `default_identical`,
`switch_identical`, `switch`, `cost_ms` (and `reason` when `not_implemented`). Critical: any length slower than 103% + 5 ms, digests that
differ under `bit_identical`, an opt-in or default mode without three identical runs, a
`not_needed` claim without three identical default runs. Advisory: no length at least 3% faster,
`not_implemented`.

## Goal 6: speculative decoding

`decision` (`shipped_opt_in` or `closed`); `cost_model` → `normal_ms`, `check_ms`, `draft_ms`,
`kept_per_check`, `estimated_speedup`. Shipped: `measured` → `speedup`, `same_session`,
`greedy_identical` (`"N/N"`), `multi_user_ratio_min`; `profile`; `drafter` → `repo`, `revision`,
`download_on_first_boot`; `active_conditions`. Closed: `reason`. Critical (shipped): speedup below
1.10, not same-session, fewer than 12 identical prompts or any mismatch, a multi-user row below
97%, a drafter that is bundled or unpinned. Advisory: the cost model's own arithmetic more than 3%
off.

## Goal 7: acceptance

`previous_release`; `ladder` → rows `{profile, isl, concurrency, before, after}` with
`decode_tok_s_u`, `output_tok_s`, `ttft_ms`; `quality_full` → `gsm8k` and `ifeval`
`{baseline, after}`; `quality_c1` → per fast-path profile `{gsm8k, ifeval}`; `smoke` → `chat`,
`context_limits` (and `tool_call`, `reasoning` when declared); `bandwidth_table`;
`conditions_matrix`. Critical: a ladder row below 97% decode or output, or TTFT above 103% + 5 ms;
GSM8K more than 1.0 or IFEval more than 1.5 points below baseline; a c=1 subset more than 5 points
below the full-set baseline; a failed smoke key; an empty table. Advisory: a c=1 subset 3-5 points
below.

## Fast checks before merging a goal

After merging a goal's branch into the integration branch, before the next goal builds on it:
decode step at width 1 (and every width the goal touched) on each served mesh, accuracy or
digests on each mesh, and one served smoke. Revert the merge if any moved more than 2% the wrong
way or an accuracy bar fails; record why.
