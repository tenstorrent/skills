# Speculative decoding (goal 6)

A small draft model guesses the next k tokens; one step of the target model checks the guesses
and the following token together; every correct guess is a token that did not cost a full step.
It helps one user at a time and only when the check step costs little more than a normal step.
Decide with numbers before building anything.

## Decide with the cost model

```
speedup = tokens_kept_per_check × normal_step_ms / (draft_ms + check_ms)
```

- `normal_step_ms`: the served batch-1 time per token (after goal 2).
- `check_ms`: the fused multi-row step at width k+1 on the rows of one sequence (goal 4). Dense:
  close to one step. MoE: `fixed + slope × union` with the union of k+1 consecutive tokens.
- `tokens_kept_per_check`: 1 + expected accepted drafts. Measure acceptance offline first by
  replaying real-text greedy generations through the drafter (prompts of the kind users send: chat,
  code, reasoning, retrieval); random tokens understate it.
- `draft_ms`: the drafter's device time per check.

```bash
python "$TT_MODEL_BRINGUP_ROOT/skills/fast-path/scripts/spec_cost_model.py" \
  --normal-ms 3.32 --check-ms 4.51 --draft-ms 0.37 --kept 1.72 --target 1.3
```

The script prints the speedup, the ceiling with perfect drafts, and what it would take to reach
the target (tokens kept, or a shorter step). Build only if the ceiling with realistic acceptance
clears the ship bar with margin; otherwise close the goal with the numbers.

On gpt-oss-20b (MoE, four chips) three drafts on a 4-row check were estimated at about 1.17× and
one draft on a 2-row check measured 1.17×: the 2-row check reads 6.4 experts instead of 9.6, so
it is cheaper and keeps fewer tokens, and the two effects nearly cancel. A dense model of the same
size would check 4 tokens for about one step's bytes; start there.

## Make the check exact

Speculation promises the text normal decoding would produce. The check's rows must compute
exactly what a batch-1 step computes, or greedy output drifts within tens of tokens:

- per-row norms computed the way the batch-1 boundary computes them;
- all rows' K/V written by one op into their shared cache tile (separate writes raced and lost
  rows on the reference model);
- each row's experts summed in that row's own rank order. An ascending-id union looked harmless
  and flipped near-ties one ulp at a time: 5 of 250 GSM8K and 13 of 100 IFEval answers differed
  until the order was fixed;
- leaving speculation mid-request must also be exact: test a forced leave.

Prove it with 12 prompts × 256 tokens and with full answer identity on the quality subsets
(GSM8K-250 and IFEval-100 gave 250/250 and 100/100 on the reference model).

## Build the drafter

- Prefer a published drafter for the target (EAGLE-3 heads exist for several models). It reads
  the target's hidden states from a few layers; expose those taps from the fused decode without
  slowing the batch-1 path.
- Run it as fused ops on the device inside the same trace as the check and the accept step, so
  the host reads one small record per step. On the reference model the drafter cost 0.37 ms per
  check and about 160 MB per chip.
- The drafter is a pointer: download its pinned revision into the HF cache on the profile's first
  boot, with a clear error when offline. Do not copy its weights into an image or bundle.

## Serve it

- Implement the TT vLLM plugin's speculative-decoding contract (`docs/SPEC_DECODE_CONTRACT.md`
  in the plugin checkout: `spec_plan`, the verify and propose calls, a `custom_class` drafter or
  `ngram`, the accept modes). The reference package predates it and patched its pinned plugin; do
  not repeat that ($vllm-integration owns plugin changes).
- Speculate only while exactly one greedy request runs, with no log-probs, penalties, logit
  bias, allowed-token lists, minimum length or structured output, with a prompt in the warmed
  range prefilled in one call, and without a prefix-cache hit (the drafter has not seen the cached
  part). A request that took a plain step does not re-enter speculation. Everything else decodes
  exactly as the non-speculative profile.
- Ship it as an opt-in profile next to the plain one. Measure what it costs: boot time (+29 s for
  the drafter and its prompt buckets), time to first token (+3 to +20 ms for the drafter's prompt
  pass), and the c > 1 rows (speculation off, within 3%).

## Ship bar

At least 1.10× tokens per second for one user on real-text prompts, measured against the plain
profile on the same image in the same session; output identical as above; c > 1 rows within 3%.
On the reference model: 305.9 → 358.6 tok/s (1.17×) on real text. On the standard random-token
benchmark it gained nothing (0.985×, about 1.45 tokens kept per check); report both.
Write `doc/fast_path/speculative.json` ([validation](validation.md#goal-6-speculative-decoding)).
