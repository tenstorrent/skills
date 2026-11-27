---
"tt-lora-train": minor
---

Add the optional `tt-lora-train` plugin. It brings up LoRA training on Tenstorrent hardware through the `tt-blacksmith` (`peft` + `torch.compile(backend="tt")`) path or the `tt-metal`/`tt-train` (TTML) path, gates every result on a fixed-window honest metric and a held-out carve that never entered training, and produces a baseline-parity table against a CUDA-trained reference that names its own negatives. It also documents the rank/tile alignment constraint and the `use_rslora` scaling rule, either of which silently invalidates a parity claim.
