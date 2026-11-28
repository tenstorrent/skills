---
name: lora-train
description: Use when training or fine-tuning a LoRA adapter on Tenstorrent hardware, or when validating whether a TT-trained adapter matches a CUDA-trained baseline. Covers bring-up on tt-metal or tt-blacksmith, the honest-metric gate, and the parity proof.
---

# LoRA training on Tenstorrent

Bring up LoRA training on Tenstorrent silicon and **prove** the result, with
numbers you can defend. This plugin exists because the ecosystem publishes
*loss curves* and no downstream *accuracy* — the gap this workflow closes.

## ⭐⭐⭐ THE RULE THIS PLUGIN IS BUILT ON

> **"Do not claim tests, model quality, hardware behavior, or performance that
> was not measured."** — Tenstorrent's own `AGENTS.md`

⇒ **Every claim carries its receipt, and the negatives are named.** A
"faster" without a number is not a claim, it is a noise. A "this was broken"
without a reproduction is not a finding, it is an opinion.

## ⭐⭐⭐ THE HONEST METRIC — READ THIS BEFORE YOU TRUST ANY NUMBER

**A single random evaluation window is not a measurement.** It can read 10x
optimistic or 10x pessimistic. The rule:

> **The score is the MEAN over a fixed set of evaluation windows — never the
> best window, never the worst, never one random window.**

| Wrong (and common) | Right |
|---|---|
| `loss = eval_loss(window_0)` on one random window | `loss = mean(loss(w) for w in FIXED_WINDOWS)` |
| report the min over N runs (looks great, is luck) | report mean ± spread over the fixed windows |
| pick the seed that scored best | fix the seed and report it |

⇒ **A metric that can improve by changing which slice you looked at is not a
metric.** Declare the window set BEFORE the run, keep it fixed, and report the
spread alongside the mean. If the spread is wide, the mean is weak — say so.

## Two gates, both mandatory

A run is not done because the loss went down. It is done when **both** gates
pass:

1. **The evaluation gate** — the honest metric (above) on a **held-out**
   carve that was never in training.
2. **The capacity gate** — the adapter's own size/rank class is respected. A
   rank-16 adapter that memorizes its training set fails this gate.

⇒ **Never advance a rung on one gate.** Two gates, both measured, or it did
not happen.

## THE PARITY PROOF — THE NUMBER THIS ECOSYSTEM LACKS

The question nobody in the ecosystem has answered:

> **Does a TT-trained LoRA reach the same quality as a CUDA-trained one?**

To answer it you need the SAME adapter, trained twice:

```
1. Train on the reference backend (CUDA) -> baseline metrics
2. Train on TT with the SAME seed, corpus, rank, alpha, target modules
3. Compare on the SAME held-out carve with the SAME honest metric
4. Report: baseline mean, TT mean, delta, and the window spread for BOTH
```

⇒ **Report the delta, not just the two numbers.** A TT mean that looks fine
but whose spread is 3x the baseline's is a different result.

## Startup

1. Resolve this plugin root from this file: two directories above its skill
   directory. Set `TT_LORA_TRAIN_ROOT` to that absolute path.
2. **Pick the backend and read its recipe** — do not mix stacks:
   - **`tt-blacksmith`** (recommended for most work) — standard HF `peft` +
     `torch.compile(backend="tt")`. Recipes:
     `recipes/tt-blacksmith/{index,env,build,train,test}.md`
   - **`tt-metal/tt-train`** (TTML) — native LoRA on TT-Metalium kernels.
     Strongest capability (rsLoRA, DPO, GRPO, FSDP) but **no pip wheel** —
     build `tt-metal` from source with `--build-tt-train`.
   - **`tt-metal` LoRA examples** — `tt-train/sources/examples/lora_llama/`
     is the canonical LoRA reference (`train_lora_llama.py`, and
     `train_lora_llama_sft.py` for the SFT path).
3. Confirm the target model, the rank, the corpus, the held-out carve, and
   whether hardware is authorized for this run. **Do not acquire hardware
   because this plugin was installed.**
4. Keep the checkout and artifacts on persistent storage. Record exact
   commands, commits, hardware identity, and evidence. **Keep weights,
   credentials, and private logs out of published commits.**

## The LoRA configuration that matters

```python
# tt-blacksmith path
from peft import LoraConfig, get_peft_model
cfg = LoraConfig(r=RANK, lora_alpha=ALPHA, lora_dropout=DROPOUT,
                 target_modules=TARGET_MODULES, task_type="CAUSAL_LM")
model = get_peft_model(model, cfg)
```
```python
# tt-train (TTML) path — note it supports rsLoRA, which stock peft does not
from ttml.modules import LoraConfig
cfg = LoraConfig(rank=RANK, alpha=ALPHA, target_modules=[...],
                 use_rslora=True,   # alpha / sqrt(rank) instead of alpha / rank
                 lora_dropout=DROPOUT,
                 is_bias_trainable=False,
                 trainable_modules=[])  # unfreeze specific non-LoRA params
```
⇒ **`use_rslora` is a Tenstorrent extension.** If your baseline used plain
`alpha/rank`, the parity comparison is invalid unless both sides match.

## ⭐ THE RANK/TILE CONSTRAINT — READ BEFORE YOU PICK A RANK

The TT-NN native tile is **32x32**, and LoRA tensors are created in
`ttnn.Layout.TILE` with shape `(1, 1, rank, in_features)`.

⇒ **Ranks that are not multiples of 32 may not map cleanly onto the native
tile layout.** r32 / r64 / r96 / r128 / r192 / r256 / r512 align; r16, r24,
r288, r432 and r528 do not.

⇒ **IF YOUR BASELINE USES A NON-ALIGNED RANK, SAY SO EXPLICITLY** in the
results table and record whether you padded the rank or used a non-tile
layout. **A padded rank is not the same adapter** — its effective capacity
differs from the baseline, and that difference must not be hidden inside a
parity claim.

## The checklist for a result

Before reporting anything, confirm all of these:

- [ ] The evaluation windows were **declared before the run** and are fixed.
- [ ] The held-out carve was **never** in the training corpus.
- [ ] The reported number is the **mean over those windows**, with the spread.
- [ ] Seed, corpus, rank, alpha, target modules, and scaling rule are
      **identical** on both sides of a parity comparison.
- [ ] The hardware identity is recorded (card, count, mesh shape, firmware).
- [ ] **The negatives are named**: what was not tested, what was not
      measured, which configurations remain unverified.
- [ ] No claim about quality, speed, or hardware behavior that was not
      measured.

## ⭐ WHAT THIS PLUGIN DOES NOT DO

- It does not migrate an existing CUDA trainer. The TTML, TT-XLA, and
  `tt-crank` paths each have their own autograd semantics; a stock PyTorch
  LoRA trainer does not transfer.
- It does not cover QLoRA. There is no 4-bit weight path in the TT stack.
- It does not cover GGUF. There is no GGUF path in the TT stack.
- It does not choose hardware for you. Device and mesh shape are set by the
  operator.

## Recipes

- `recipes/tt-blacksmith/` — the `peft` + `torch.compile(backend="tt")` path
- `recipes/tt-metal/` — the `tt-metal/tt-train` (TTML) path

Read the matching recipe before acting. Record the exact commit you used.
