# Roofline and baseline (goal 1)

Decode at batch 1 reads every active weight once per token, and compute is idle most of the time.
The step time is therefore bytes read per chip divided by the bandwidth the kernels achieve. Two
levers exist and nothing else: read fewer bytes, or read them closer to peak. Measure both before
writing kernels.

## Account the bytes

Per token, per role, at the formats the datatype sweep selected:

- attention: Q, K, V and output projections (plus biases) per layer;
- dense MLP: gate, up and down per dense layer (two matrices for an ungated MLP);
- MoE: `experts_per_token × (gate + up + down)` per MoE layer, plus the router and any shared
  experts; dense layers before the first MoE layer count as dense;
- LM head: `hidden × vocab`, at the vocabulary width the tensor really has (padding counts);
- KV cache: `2 × kv_heads × head_dim × context` per full-attention layer (sliding-window layers
  read at most the window). It is under 1% at 128 tokens of context and can dominate at 8k; report
  the context with every number.

Bytes per value: BF16 2.0; BFP8 1.0625 (one shared exponent per 16 values); BFP4 0.5625;
MXFP4 checkpoints 0.53125. Per chip, divide by the tensor-parallel width when every role is split
evenly; record any role that is replicated instead.

`skills/fast-path/scripts/fast_path_roofline.py` does this from an HF `config.json`:

```bash
python "$TT_MODEL_BRINGUP_ROOT/skills/fast-path/scripts/fast_path_roofline.py" \
  --config <checkout>/config.json --tp 1 2 4 --format experts=bfp8 attention=bfp8 lm_head=bfp8 \
  --context 128 --measured-ms 1=<ms> 4=<ms> --json doc/fast_path/roofline_calc.json
```

It prints GB per token, GB per chip, the DRAM floor (`GB per chip / peak`) and, with a measured
step, the achieved GB/s and % of peak. Check it against a hand count of one layer; architectures
it does not know need `--override` values.

## Measure the baseline

- Device step: traced batch-1 decode, mean over at least 128 tokens after warmup, at a short
  context (ISL 128) and a long one (the context contract's long benchmark length or 8k), quiet box
  ($tt-device-usage). Use the reduced-variant derivation from $optimize when profiling.
- Served: the stage 10 harness at concurrency 1 (TPOT, TTFT) for the same profile. The ratio of
  TPOT to device step is the host overhead; above about 5% it is its own lever.
- Each QuietBox 2 mesh the model serves: 1x4 (four chips), 1x2 (one board), 1x1 (one chip).
- DRAM budget per device at the served profile: weights, KV pool, trace region, free bytes. The
  free bytes decide whether decode-only weight copies fit or the decode must stream the prefill
  layout (see [mesh variants](mesh-variants.md)).

## Read the result

| % of peak at batch 1 | Meaning |
|---|---|
| < 30% | launch- and glue-bound. Goal 2 is the lever; expect 2-4×. |
| 30-60% | the matmuls stream, the rest does not. Fuse boundaries and the terminal path. |
| 60-90% | near the streaming ceiling. Bytes (formats, padding, what is read) are the lever. |
| > 100% | the accounting is wrong: a role is missing, replicated, or reads less than assumed. |

Reference points on Blackhole (gpt-oss-20b, all BFP8, measured): the stock decode reached 18% on
four chips and 38% on one; the fused decode 55% and 81%; its expert kernels alone stream at
about 95% of peak on one chip. Four chips trail one chip because every layer pays two all-reduces
and their syncs.

## Pick a bar

Without another implementation to beat, the bar is the floor: aim for at least 50% of peak on
four chips and 75% on one, and treat the gap to the floor as the backlog. When another
implementation exists for the model (a bare-metal runner, a vendor demo), measure it on the same
box with its own protocol, and state its formats beside its number. A faster step from fewer bytes
is not a faster engine; the [case study](case-study-gpt-oss.md) shows both readings.

## Write `doc/fast_path/roofline.json`

Fields are listed in [validation](validation.md#goal-1-roofline). Include the plan: each lever, the
bytes or bandwidth it changes, the estimated gain (labelled as an estimate) and its risk.
