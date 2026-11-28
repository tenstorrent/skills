# TT LoRA Training and Parity Proof

Train LoRA adapters on Tenstorrent hardware and **prove** the result.

This plugin exists to close a specific gap in the Tenstorrent ecosystem:
training publishes **loss curves**, but no **downstream accuracy** and no
**baseline parity**. The ecosystem's own rule is the reason:

> "Do not claim tests, model quality, hardware behavior, or performance that
> was not measured."

## What it does

- **Bring-up** LoRA training through either the `tt-blacksmith` path (stock HF
  `peft` + `torch.compile(backend="tt")`) or the native `tt-metal/tt-train`
  (TTML) path, which adds rsLoRA, DPO, GRPO, FSDP and pipeline parallelism.
- **Gates** every result on a **fixed-window honest metric** (the mean over a
  declared, fixed evaluation set — never the best window, never one random
  window) and a **held-out carve** that never entered training.
- **Produces a parity table**: the same adapter, same seed, same corpus, same
  rank and scaling rule, trained on both backends and compared on the same
  held-out set — with the delta and the spread for both sides.
- **Names its own negatives**: what was not tested, what remains unverified,
  and any rank that does not align with the native 32x32 tile.

## Install

```bash
codex plugin add tt-lora-train@<marketplace>
```
```bash
/plugin install tt-lora-train@<marketplace>
```

This plugin has **no dependency on any other Tenstorrent plugin**. It is
self-contained.

## What it does not do

- It does not migrate an existing CUDA LoRA trainer. The TTML, TT-XLA and
  `tt-crank` paths each have their own autograd semantics.
- It does not cover QLoRA. There is no 4-bit weight path in the TT stack.
- It does not cover GGUF. There is no GGUF path in the TT stack.
- It does not select hardware for you. Device and mesh shape are the
  operator's decision.

## The honest metric, in one table

| Wrong | Right |
|---|---|
| one random evaluation window | mean over a **fixed, pre-declared** window set |
| report the min over N runs | report the mean **and the spread** |
| pick the seed that scored best | fix the seed and report it |

A metric that can improve by changing which slice you looked at is not a
metric.

## Layout

```
plugins/tt-lora-train/
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  README.md
  skills/lora-train/SKILL.md
  recipes/tt-blacksmith/index.md
  recipes/tt-metal/index.md
```

## License

Apache-2.0.
