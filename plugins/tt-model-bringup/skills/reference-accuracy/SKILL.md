---
name: reference-accuracy
description: Prove the full model computes what the HF reference computes, on a frozen CPU reference corpus, and keep that corpus as the accuracy regression gate for every later change. Use for goal 6b, for the quick/full/long/served tiers in later stages, and for goal 12's per-task reference check.
---

# Reference accuracy

One question: does the TT model compute what the HF model computes? Precision choice is
`datatype-sweep`'s job and performance is `optimize`'s; this skill only measures and fixes correctness.

## Reference corpus

HF in bf16 on the host CPU, teacher-forced; store top-k logits (k ≥ 20) and the token ids, both hashed.
The CPU only teacher-forces, never generates: free CPU generation runs at a few tokens per second.
Slices are fixed texts (public reasoning and agent transcripts, rendered with the model's chat template)
or the TT model's own generations. Slices: `reasoning` (long thinking traces), `agentic` (multi-turn
tool-call conversations), `long` (one document at ≥ 32K tokens, or the longest that fits), `stress` (loop and repetition prompts),
`on_policy` (the TT model's own generations), `held_out` (checked at milestones only, never tuned on).
Everything but `on_policy` needs only the checkpoint: start it in the background at goal 1 with
capped threads, pause it during performance measurements, write results incrementally and its state to
`doc/reference/corpus_status.json`; any later goal resumes it from there, and goal 6b finishes what is
missing (each goal is a separate session, so a background process may not survive). If the model
does not fit in host RAM, use a GPU reference and say so. Token ids are frozen once written.

## Comparison

Feed identical tokens to HF and TT. Per position: KL(HF‖TT) on the top-k, top-1 agreement, HF
log-probability of TT's chosen token; on `stress`, HF probability of the loop's first token. Report
mean and p95 per slice. Tolerance: the larger of 3× the HF bf16 vs fp32 spread on a sample and a small
floor (KL p95 0.02, top-1 1 point), so TT's own bf16 accumulation is not read as a defect.

Run two TT configurations:
- `current`: the precision the model has now. Recorded as the baseline; never changed here.
- `full`: the highest precision that fits: BF16 weights and activations, HiFi4, BF16 KV cache and head;
  if that does not fit, full precision per group or layer range; else per layer on HF activations;
  else BFP8 weights with HiFi4. Record which, and the ops that could not reach it.

## When `full` differs from HF

A divergence is a slice outside tolerance, or a cluster of positions, that localises to a layer or op;
isolated near-tie top-1 flips are not. A divergence at full precision is an implementation defect.
Localise it by layer, then op, with HF activations; fix the model code, or the tt-metal op when a minimal repro proves the op wrong (fix it there and run its tests);
then rerun the affected slices and the stage's own tests. A reference problem (tokenizer, chat
template, config) is fixed in the corpus. `current` far from HF while `full` matches is quantization:
record the per-group attribution if cheap and leave it to `datatype-sweep`.

## Outputs

`doc/reference/reference.json`:
- `reference`: model, revision, dtype, device;
- `slices`: per slice `name`, `kind`, `positions`, `tokens_sha256`, `logits_sha256`;
- `full`: `meaning`, `unreached_ops`, `per_slice` {`kl_mean`, `kl_p95`, `top1`};
- `current`: `precision`, `per_slice` (same fields); `tolerance` {`kl_p95`, `top1`} with its source;
- `verdict`: `match` or `divergence`; `divergences`: `where`, `cause`, `fix`, `status` (`fixed`, `open`);
- `tiers`: `quick` (≈1–2K positions, minutes), `full` (all but `held_out`), `long`, `served`, each a
  command; `baseline`: precision and per-slice numbers later changes are gated on.

`readiness/reference_check` (or the model's equivalent) runs any tier, or a stage-12 trajectory window,
and writes the same per-position fields. `doc/reference/REPORT.md` leads with the verdict.

## Regression gate (later stages)

A change passes when, per slice, `kl_p95` ≤ 1.10 × baseline + tolerance and `top1` ≥ baseline − 2 points,
and batched equals alone. Quick tier on every change, full at milestones, long for attention, cache and
prefill changes, served for vLLM changes. The baseline moves only on `datatype-sweep`'s selection or an
accepted, recorded tradeoff.
