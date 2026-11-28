# tt-blacksmith — the `peft` + `torch.compile(backend="tt")` path

**Use this path by default.** It uses stock HuggingFace `peft`, so an
existing LoRA recipe transfers with minimal change, and it is the path
`tt-blacksmith`'s own training recipes use.

## What this path is

```python
# blacksmith/models/hf_models.py
from peft import LoraConfig as PeftLoraConfig
from peft import get_peft_model
...
if config.lora is not None:
    model = _apply_lora(model, config.lora)
else:
    for param in model.parameters():
        param.requires_grad = True
```
```python
# blacksmith/tools/trainer/strategies/crank_llm_trainer.py
if self.config.use_tt:
    self._compute_loss_fn = torch.compile(
        compute_causal_lm_loss, backend="tt", dynamic=False,
        options=tt_compile_options(self.config),
    )
```

⇒ **LoRA is OPT-IN over full fine-tune.** Without a `lora` config block the
trainer does a full fine-tune. If you intend LoRA, say so explicitly in the
config — otherwise you will silently train the whole model.

## Environment

```bash
# 1. the system stack (tt-smi, tt-flash, packages)
/bin/bash -c "$(curl -fsSL https://github.com/tenstorrent/tt-installer/releases/latest/download/install.sh)"

# 2. confirm the device is visible
tt-smi

# 3. the tt-metalium container (1GB for TT-NN; 10GB for a full build)
tt-metalium          # launches with $HOME mounted
tt-metalium python3  # run inside the container
```

⇒ **READ THE WARNING in the tt-installer README** — it pipes a remote script
into a shell. If your operator has a policy against that, install the
packages manually from the Tenstorrent repositories instead.

## The checkout

```bash
git clone https://github.com/tenstorrent/tt-blacksmith.git
cd tt-blacksmith
```

## The crank wheel

`tt-blacksmith` uses the `tt-crank` backend, whose wheel is a **nightly CI
artifact** of `tt-mlir`, fetched by:

```bash
bash env/download_crank_wheel.sh
```

⇒ **THIS IS A NIGHTLY BUILD, NOT A STABLE RELEASE.** Record the exact wheel
and the `tt-mlir` commit you ran against. A parity number produced on one
nightly wheel is not automatically reproducible on the next.

## Configure LoRA

```yaml
lora:
  r: 16
  alpha: 32
  dropout: 0.05
  target_modules: ["q_proj", "v_proj"]
  task_type: "CAUSAL_LM"
```

⇒ `r`, `alpha`, and `dropout` are all `> 0` constrained in the config model.
⇒ **`target_modules` must match the model's actual module names.** A pattern
that matches nothing fails loudly in the `tt-train` path
(`"[LoRA] target_modules pattern(s) matched no LinearLayer in the model"`) —
read that error as "your pattern is wrong", not as "LoRA is broken".

## Run

```bash
# single device
python3 -m blacksmith.tools.trainer.strategies.crank_llm_trainer <config>
# or use the repo's own experiment entrypoint for the model you want
```

⇒ **Read the recipe for your target model before running it.** The repo
carries per-model experiments with their own configs and known-good
hyperparameters.

## Test

```bash
pytest tests/ -v
```

## Measure — the part that matters

The trainer emits loss curves. **A loss curve is not a quality claim.**

```bash
# 1. declare the evaluation windows BEFORE the run and keep them fixed
# 2. evaluate on a held-out carve that never entered training
# 3. report mean over the fixed windows, plus the spread
```

⇒ **The `tt-model-bringup` plugin's `lm-eval` profiles** (`mmlu_pro`,
`gsm8k_cot`, `ifeval` — see the `tt-model-bringup` runtime) are the closest
existing harness for downstream accuracy. Use them rather than inventing one,
and report the sample counts, because they are small and the spread will be
wide. **A 256-sample `gsm8k_cot` number is noisy — say so when you report it.**

## Record

- `tt-blacksmith` commit, `tt-mlir` commit, crank wheel version
- hardware identity: card model, count, mesh shape, firmware version
- seed, corpus, rank, alpha, target modules, scaling rule (`alpha/rank` or
  `alpha/sqrt(rank)`)
- the fixed evaluation window set, and the spread
- **the negatives: what was not tested, what remains unverified**
