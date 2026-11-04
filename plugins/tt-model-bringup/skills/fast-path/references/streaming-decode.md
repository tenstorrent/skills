# Streaming decode (goal 2)

Replace the stock batch-1 decode on the primary mesh (1x4 on a QuietBox 2) with a short chain of
`ttnn.generic_op` programs. Start from `models/demos/gpt_oss/tt/fused_decode/` in the target
checkout. Its README walks one token through the programs; this guide says what to keep, what to
adapt, and how to prove each piece.

## The program set

| Program | Does | Replaces |
|---|---|---|
| inputs | writes the token's embedding row and the RoPE cos/sin rows in one op | about ten small ops |
| boundary | all-reduce of the previous partial sums + residual add + next RMSNorm, run inside the next matmul's program | CCL + add + norm ops and their launches |
| linear stream | DRAM-streaming matmul for QKV, output projection, router, LM head; each DRAM bank feeds its own readers in large bursts; bias and epilogues fused | DRAM-sharded or interleaved matmuls |
| expert streams (MoE) | gate/up and down that read only the routed experts' weights, with activation, gating and the weighted sum fused; top-k and softmax run in the router's writer | sparse matmuls, top-k, typecast, tilize, pad |
| attention | fused Q/K RoPE + paged SDPA decode on the stock op where it already streams | — |
| terminal | final boundary + norm inside a streamed LM head; per-core top-32 candidates, a merge and a cross-device exchange; the greedy pick writes the next token into the decode input | LM head, all-gather, top-k, argmax, host readback |

Between layers the residual is one flat BF16 vector on one core, replicated per device; the
boundary owns that contract. Every program reads its inputs from where the previous one wrote
them, so one decode step is a fixed set of launches with no host work.

## Kernel rules that made the difference

- Spread each weight over all DRAM banks with an explicit per-bank layout written at load time,
  and give each bank enough readers to stay saturated. Sweep readers per bank per role (one to
  three); the best count differs between narrow and wide matrices and between meshes. Uneven
  splits are fine when a role's width does not divide evenly.
- Keep the per-step constants (router weights, norm weights, index tables, semaphores) in
  persistent tensors built before capture. Nothing is created per step.
- Fuse what touches the activation between two weight streams into the stream's writer or the
  next program's reader. A launch costs microseconds; at 3 ms per token with eight programs per
  layer, launches and syncs are a visible share.
- Pad tiles only where the hardware needs them and drop padded columns at the source; padding
  that is streamed is bandwidth spent on zeros.

## Decode-only weights

The streaming layout rarely matches the prefill layout. Two options:

1. **Decode copies:** keep prefill's tensors, add per-bank decode copies of the streamed roles.
   gpt-oss-20b paid about 3.2 GB per device with 4-bit experts and 2.7 GB more with BFP8 experts on
   four chips. Check the KV pool and the context contract after adding them; if they shrink,
   option 2 is required.
2. **Shared layout:** store the weights once in a layout both paths read (for experts: one DRAM
   shard per expert and output tile column), stream it in decode and read it through an indexed
   sparse matmul in prefill. This is mandatory on one chip ([mesh variants](mesh-variants.md)).

Record the bytes either way, put the layout in the weight-cache key, and record it in the cache's
completion marker so a cache built without the copies is rebuilt instead of loaded.

## Sampling and fallbacks

The terminal's candidate path is greedy (and top-k up to the candidate width). Requests with
penalties, log-probs, grammar masks or a wider top-k convert the fused logits to the stock layout
on the device and use the common sampler; route them per step in the generator, and keep the
sampler trace keys distinct ($optimize, $tt-enable-tracing).

## Serving as a decode bucket

- Add the fused decode as the batch-1 decode bucket of `generator_vllm.py`; leave every other
  bucket on its existing path. Declare the buckets in `tt_supported_decode_batch_sizes`
  ($vllm-integration).
- Gate it on a switch (environment variable or TT config key) that defaults on where it is
  supported, and log the decode path once at boot so a served log proves which path ran.
- Prepare the fused programs, persistent buffers and every prefill bucket before the first capture;
  check the allocation tracker and the program-cache count across a mixed workload
  ($tt-enable-tracing). On the reference model, a prefill length first seen after capture corrupted
  a cached RoPE slice and a program compiled after capture hung the device; both were preparation
  defects.

## Prove it, smallest piece first

1. Each program against the stock op it replaces, with real weights, on one device: PCC per output
   and the exact top-k ids for the router and terminal.
2. Two layers against HF on the target mesh, a few decode steps.
3. Full model: teacher-forced top-1/top-5/PCC against the HF reference at the same positions as the
   baseline; 2,000-token generations; 200 back-to-back requests; a 2 × 4,000-step back-to-back run
   compared with a synced run (it caught nothing on the reference model, and it is how a reviewer's
   race concern was answered).
4. Device step at short and long context, quiet box; served c=1 TPOT and TTFT; the KV pool and the
   accuracy gates in [validation](validation.md#goal-2-streaming-decode).

Keep the per-op check scripts with the model (the reference package ships them next to the
programs); every later goal reruns them after touching a shared kernel.
