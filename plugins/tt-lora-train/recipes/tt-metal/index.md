# tt-metal / tt-train — the native TTML path

**Use this path when you need the capabilities the `peft` path lacks:**
rsLoRA, DPO, GRPO, FSDP, pipeline parallelism, native device optimizers.

**Cost to entry: higher.** `tt-train` has **no pip wheel** — you build
`tt-metal` from source with tt-train enabled.

## Build

```bash
git clone https://github.com/tenstorrent/tt-metal.git
cd tt-metal
git submodule update --init --recursive

./build_metal.sh --build-tt-train              # release
./build_metal.sh --build-tt-train --debug
./build_metal.sh --build-tt-train --development
```

⇒ **Record the `tt-metal` commit.** This path is source-built, so the
version IS the commit.

## The canonical LoRA reference

```bash
# the LoRA example (toy Shakespeare by default)
python3 ./sources/examples/lora_llama/train_lora_llama.py --batch 8 --steps 200

# with pretrained weights — the architecture comes from the checkpoint's config.json
python3 ./sources/examples/lora_llama/train_lora_llama.py \
    -m ./configs/model_configs/tinyllama_bpe.yaml \
    --pretrained TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
    --batch 8 --steps 200
```

⇒ **The YAML only sets training-time knobs** (`max_sequence_length`,
`runner_type`, `dropout_prob`) when `--pretrained` is used — the
architecture and vocabulary come from the checkpoint's `config.json`.
⇒ **Loading FAILS rather than silently cropping or padding** if a tensor's
shape disagrees with `config.json`, or if the tokenizer has more tokens than
the checkpoint has embedding rows. **Read that failure as a real mismatch,
not as a bug in the loader.**

### Multi-device

```bash
# data parallel over 8 devices of a Wormhole Loud Box (T3K)
export TT_MESH_GRAPH_DESC_PATH=/path/to/t3k_1x8_mesh_graph_descriptor.textproto
python3 ./sources/examples/lora_llama/train_lora_llama.py \
    -m ./configs/model_configs/tinyllama_bpe.yaml \
    --pretrained TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
    --mesh_shape 1,8 --dp_axis 1 --batch 8 --steps 200

# tensor parallel over two devices
--mesh_shape 1,2 --tp_axis 1
```

⇒ Under tensor parallelism the token embedding is **vocab-sharded** — it is
the only layout in which tied embeddings (e.g. Llama 3.2 1B) can load.

## The SFT path

```bash
python3 ./sources/examples/lora_llama/train_lora_llama_sft.py --batch 8 --steps 200
```

⇒ This delegates to `SFTTrainer`, which is **TRL-shaped**:
`gradient_accumulation_steps`, `max_grad_norm`, `warmup_steps`,
`gradient_checkpointing`, `compute_loss_func`, and callbacks
(`on_step_end`, `on_before_optimizer_step`, `on_eval_end`, `on_save`).

## The LoRA implementation

```python
# sources/ttml/ttml/modules/lora.py
@dataclass
class LoraConfig:
    rank: int = 8
    alpha: float = 16.0
    target_modules: list[str] = field(default_factory=list)
    use_rslora: bool = False        # alpha / sqrt(rank) instead of alpha / rank
    is_bias_trainable: bool = False
    trainable_modules: list[str] = field(default_factory=list)
    lora_dropout: float = 0.0
```

⇒ **`use_rslora` is a Tenstorrent extension** stock `peft` does not have.
⇒ **If your CUDA baseline used plain `alpha/rank`, a TT run with
`use_rslora=True` is NOT a like-for-like comparison.** Match the scaling rule
on both sides or the parity claim is invalid.
⇒ `trainable_modules` unfreezes specific non-LoRA parameters (e.g. layer
norms) by name substring — useful for a fair comparison if the baseline
unfroze them too.

## The rank/tile constraint

`_create_lora_A` makes a tensor of shape `(1, 1, rank, in_features)` in
`ttnn.Layout.TILE`. The native tile is **32x32**.

⇒ **r32 / r64 / r96 / r128 / r192 / r256 / r512 align. r16, r24, r288, r432,
r528 do not.**
⇒ **IF YOUR BASELINE RANK DOES NOT ALIGN, SAY SO** and record whether you
padded the rank or used a non-tile layout. A padded rank is not the same
adapter — its effective capacity differs, and that difference must not be
hidden inside a parity claim.

## Distributed

```bash
# multi-host pipeline parallelism over fabric
# configs/multihost_configs/: 1worker_fabric.yaml ... 16worker_126block_pp_fabric.yaml
# mesh/graph descriptors: configs/mgd/bh_galaxy_32_1_ring_ring.textproto, etc.
```

⇒ FSDP: `ttml.fsdp.fully_shard(block)` — the API is intentionally familiar
to PyTorch FSDP2.
⇒ Optimizers with native device kernels: `adamw`, `sgd`, plus `MorehAdamW`
and `Muon`.

## Checkpointing

LoRA checkpoints are **safetensors**, and loading is **strict** (unknown and
missing keys both raise).

```python
def save_lora_checkpoint(model, path, step):
    """Save only the trainable (LoRA) parameters to a safetensors file."""
    from safetensors.numpy import save_file
    tensors = {name: param.to_numpy(ttnn.DataType.FLOAT32)
               for name, param in model.parameters().items()
               if param.get_requires_grad()}
    filepath = os.path.join(path, f"lora_step_{step}.safetensors")
```

⇒ **Resume is supported** via `--resume <path to a LoRA checkpoint
.safetensors>`.

## Test

```bash
pytest tests/ -v
```

⇒ **Known upstream issues to check against, not to work around blindly:**
`tt-xla#1993` and `tt-xla#4809` (MeshDevice re-open crashes) affect the XLA
path and force fused-JIT step functions and a manual one-hot cross-entropy.
The TTML path is separate.

## Record

- `tt-metal` commit (the source build IS the version)
- hardware identity, mesh shape, graph descriptor used
- seed, corpus, rank, alpha, scaling rule, target modules
- the fixed evaluation window set, and the spread
- **the negatives: what was not tested, what remains unverified**
