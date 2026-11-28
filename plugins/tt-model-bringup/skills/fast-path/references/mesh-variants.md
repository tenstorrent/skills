# Mesh variants (goal 3)

A QuietBox 2 serves the same model on three meshes: 1x4 (all four chips, a ring), 1x2 (one board,
a line) and 1x1 (one chip). Each needs its own decision: fused by default, fused but opt-in, or
stock with a measured reason. The per-device math changes with the tensor-parallel width; the
programs mostly do not.

## Opening the mesh

- One chip is a true one-device mesh selected with `TT_METAL_VISIBLE_DEVICES=<n>`, never a submesh
  of the 1x4 ($tt-device-usage). `ttnn.get_num_devices()` still reports four on a QuietBox 2, so
  do not gate support on "the mesh is the whole system".
- Two chips use one board's pair with the board's mesh graph descriptor and a 1-D line fabric;
  four chips use the ring. The boundary all-reduce and the terminal's candidate exchange follow
  the topology: one hop to the neighbour on two chips, none on one.

## One chip (TP=1)

The first constraint is DRAM, not speed. On gpt-oss-20b the BFP8 experts alone take about 20 GB
of a P150's 32 GB, so there is no room for decode copies:

- Store the experts once in a layout both paths can read: one DRAM shard per (expert, output tile
  column), placed round-robin over the banks. Decode streams the routed experts' shards; prefill
  and the batched decode read the same tensors through an indexed `sparse_matmul`, bit-identical
  to the old layout. A padded variant was tried and rejected: +8% prefill and out of memory at 4k.
- Give only the dense roles (QKV, output projection, router, LM head) decode copies, about 1.3 GB.
- Drop the all-reduce from the boundary and the fabric exchange from the terminal; keep the rest.
- Watch tile-shape assumptions that held at four chips: all Q heads now live on one device (64
  heads span two tile rows), and the sampler sorts candidate tiles in pairs, so its candidate row
  needs two tiles.
- Run expert prefill on a large grid (about 90 cores); the default 12- or 30-core grids left half
  the chip idle and doubled prefill time.

Result on the reference model: 20.62 → 9.43 ms per token, 81% of peak, teacher-forced top-1
96.9% against 94.3% for the stock path. The single-chip variant is upstream as a draft in
tenstorrent/tt-metal #61225.

## Two chips (TP=2)

Apply the four-chip layouts at twice the width per device: half the Q and KV heads, the expert
intermediate padded to a multiple of the per-bank tile split, half the vocabulary per LM head.
Decode copies fit here (11.9 GB per device on the reference model) and the KV pool kept its size.

Measure the other workloads after adding the path, not only batch 1. On the reference model the
two-chip fused decode ran 13.74 → 5.39 ms and 73 → 187 tok/s per user, but building it slowed the
stock eight-user decode by 0.2 ms per step (3% of throughput). A same-size DRAM pad did not
reproduce the slowdown, so the cause was not placement, and it was not found. The result shipped as
the default of the latency profile and stayed off on the throughput profile. That is the opt-in
rule in practice.

## Four chips (TP=4)

The reference implementation's home mesh. Two all-reduces per layer and their syncs are why it
reaches about 55% of peak where one chip reaches 81%; the boundary program's fusion and its link
count are the levers that remain.

## Record per mesh

For each mesh: status (`fused`, `opt_in`, `stock`, `unsupported`), the reason when not fused,
device step before/after at short and long context, accuracy before/after, the weight layout
(decode copies or shared), DRAM cost and KV blocks before/after, reader counts per role, and the
profile that enables it. Write `doc/fast_path/mesh_variants.json`
([validation](validation.md#goal-3-mesh-variants)).
