# Precision on reasoning text (probe P1)

Short top-1/top-5 references are 100 greedy tokens with large margins. Compressed
weights pass them and still lose eval points on long reasoning answers. Evidence:

- MoVA: BFP4 dense layers passed top-5 on 100 tokens; CI GPQA 38.4 against a card
  value of 80.8. Teacher-forced on a long reasoning trace, that policy matched HF
  top-1 on 85.2% of tokens and dense BFP8 on 96.1%. Dense BFP8 scored 79.3, 3–5% slower.
- Gemma-12B: KL×1000 against HF on GPQA traces was 74 with the BFP4 exceptions,
  28 without them, and 7.7 with an exact (tanh) GELU instead of the fast one.
  GPQA moved 66.7 → 69.4 → 72.9.
- Qwen-Flash: one layer group from BFP4 to BFP8 gave +3 GPQA points.

P1 measures exactly this, on the model's generator, before vLLM.

## Run it

1. Write `candidates.json`: candidate id → `build_generator` kwargs, the same
   object you would pass with `--build-kwargs`. At most 5. Order matters only
   for the default reference (the first one).

   ```json
   {
     "bfp8": {"precision_config": "doc/datatype_sweep/configs/bfp8.json"},
     "bfp4_mlp_lofi": {"precision_config": "doc/datatype_sweep/configs/bfp4_mlp_lofi.json"},
     "bfp4_ff1ff3_lofi": {"precision_config": "doc/datatype_sweep/configs/bfp4_ff1ff3_lofi.json"}
   }
   ```

   Offer only candidates that fit with the full-context KV budget (see "Memory fit
   first" below). The reference is the highest-precision one that fits.

2. Run from the tt-metal checkout with the readiness runtime on `PYTHONPATH`:

   ```bash
   python -m readiness_check.run_precision_reasoning \
     --model-dir models/autoports/<model> --hf-model <org/name> \
     --mesh-device <mesh> [--fabric-config FABRIC_1D_RING] \
     --candidates models/autoports/<model>/doc/datatype_sweep/candidates.json \
     --reference bfp8 --selected <selected id>
   ```

   - The first run generates about 30 answers (`--num-texts`, up to
     `--max-new-tokens 6144` each) with the reference config, from the bundled
     questions in `runtime/readiness_check/reasoning_prompts.json`, chat
     template with thinking on (`--chat-template-kwargs`, default
     `{"enable_thinking": true, "thinking": true}`).
     Model-card sampling (`generation_config.json`, or `--sampling`) is passed
     to `generate()` only for the keywords it declares (`temperature`, `top_p`,
     `top_k`, `seed`). Without them the answers are greedy and loops are dropped;
     add those keywords to `generate()` if most answers are dropped.
   - Or bring traces: `--text-file traces.jsonl`, one row per answer, either
     `{"prompt_token_ids": [...], "completion_token_ids": [...]}` or
     `{"messages": [...], "response": "<thinking and answer text>"}`.
   - Token ids are cached in `doc/eval_readiness/precision_reasoning/text.jsonl`
     and each candidate's scores in `.../scores/<id>.pt`; reruns score only what
     changed. Delete `text.jsonl` to regenerate the text.
   - If building several generators in one process fails (device memory not
     released), run one candidate per process with `--only <id>`; the last run
     aggregates the cache.
   - `--build-kwargs` / `--build-kwargs-file` apply under every candidate's kwargs
     (for example `max_seq_len`).

3. The gate `scripts/check_eval_readiness.py --stage datatype-sweep` reads
   `doc/eval_readiness/precision_reasoning.json`; the runner calls it after the stage.

## What it measures

Per answer position, one `prefill_logits` call per text gives:

- ΔNLL: NLL of the actual next token, candidate minus reference. For text sampled
  from the reference at temperature 1, its mean equals KL(reference ‖ candidate).
- KL×1000: KL(reference ‖ candidate) over the reference's top-64 tokens plus one
  tail bin. This is a lower bound of the full KL and close to it.
- top-1 agreement with the reference argmax, and Δtop-1 against the text token.

Standard errors are cluster-robust over texts: positions inside one answer are
correlated, so each text is one cluster (with fewer than 8 texts, 128-position
blocks). `--decode-tokens 256` on `--decode-texts 4` also runs traced decode
teacher forcing over the last tokens of each text, to cover decode-only weights
and fidelities.

`--hf-positions 2048` scores the reference against the original HF model on the
CPU (bf16) when the checkpoint fits in free host RAM; otherwise the check is
skipped with the reason. This catches a loss the reference itself has.

## Checks and defaults

| Check | Severity | Passes when |
|---|---|---|
| `positions_scored` | critical, not waivable | ≥ 1,000 paired positions (`--min-positions`) |
| `selected_within_noise` | critical, waivable | ΔNLL ≤ max(2·SE, 0.01 nats/token) and KL×1000 ≤ 15 |
| `decode_top1_within_noise` | advisory | decode Δtop-1 ≥ −max(2·SE, 0.01) |
| `reference_vs_hf` | advisory | KL(reference ‖ HF)×1000 ≤ 15 |

The 0.01 floor (KL×1000 = 10) and the KL bound 15 sit between Gemma-12B's
exact-GELU policy (7.7 against HF) and its no-BFP4 fast-GELU policy (28, about
3.5 GPQA points). They are defaults; the owner can tune `--nll-floor`,
`--kl-max-x1000` and `--hf-kl-max-x1000`. `data.candidates` in the result holds the
verdict for every candidate, so the sweep can use P1 per weight group.

## Usual fixes when a check fails

- `selected_within_noise`: restore the most sensitive BFP4 group to BFP8 (restore
  order in the skill), rerun P1 for the new candidate only (`--only`), repeat.
- `reference_vs_hf`: the reference itself drifts. Check approximate activations
  (fast GELU / SiLU / exp), BFP weights left in the reference, LoFi on
  accuracy-sensitive matmuls, BF16 vs FP32 accumulation, and the LM head dtype.
- `positions_scored`: too few answers survived. Raise `--num-texts` or
  `--max-new-tokens`, add sampling keywords to `generate()`, or pass `--text-file`.
- Build or scoring errors appear in `data.errors`; fix them like any generator bug.

## Memory fit first

Before writing `candidates.json`, read `doc/context_contract.json` and the stage-6
memory reading: a candidate that does not fit the full advertised context with
its KV cache is not offered. More precision also means less free DRAM for the KV
pool and so fewer concurrent long answers (Qwen-Flash: 125 → 95 MB free per bank);
record the free-DRAM change in the sweep README; P4 at stage 10 measures its cost.

If BFP4 is the only option that fits, keep it, let P1 report the loss, and record a
limitation in `doc/eval_readiness/limitations.json`:

```json
{"items": [{"probe": "precision_reasoning", "check": "selected_within_noise",
  "reason": "required to fit: BFP8 experts leave no room for the full-context KV cache (see doc/context_contract.json)",
  "evidence": ["doc/eval_readiness/precision_reasoning.json", "doc/context_contract.json"],
  "original_model": "not_run", "fix_hours": 2.0}]}
```

The gate accepts a `not_run` waiver only with `fix_hours` ≥ 2: spend the time box looking for a
fitting higher-precision option (one group at BFP8, a smaller KV block budget) and record it. The
gate then reports the loss as advisory, so the eval risk is known before CI.
