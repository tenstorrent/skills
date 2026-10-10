# Tracing a ttnn call to its device op

Goal per call: device op struct (the Tracy `OP CODE`, e.g. `MatmulDeviceOperation`), `prim::`
name, program factory, input and output shape/dtype/layout/memory, launches per inference, and
evidence (file basename plus function or identifier).

## Procedure

1. Find the binding: `git grep -n "<api name>" <sha> -- ttnn/cpp ttnn/ttnn`.
2. Follow the C++ entry to every `ttnn::prim::` call and every helper that launches one
   (`to_memory_config`, `to_layout`, `reshape`, `concat`, `move`).
3. In the device operation, find `select_program_factory` and evaluate it for this input.
4. Note conditions that depend on runtime state (free L1, allocator addresses) and mark the row
   `unverified` with the condition in `evidence`.

## Known traps (each caused a wrong count in a previous analysis)

- `ttnn.reshape` on ROW_MAJOR with the same last dim is a view: no launch.
- `ttnn.to_memory_config` returns the input unchanged when the configs compare equal: no launch.
  `ttnn.reshard` has no such check: it always launches.
- `to_layout(TILE, dtype=...)` with padding is one `tilize_with_val_padding`, not tilize + typecast.
- `ttnn.add`/`ttnn.mul_` all go to `binary_ng`; activations listed in the call are fused.
- `ttnn.linear` with only `core_grid` builds its own program config; bias is fused when a config
  or core grid is given.
- `split_query_key_value_and_split_heads` on a sharded input goes to `create_qkv_heads`;
  `concatenate_heads` goes to `nlp_concat_heads`.
- conv2d: 1x1 with stride 1 and no padding runs as `prim::matmul` (1D mcast for height-sharded,
  2D mcast for block-sharded input); every other conv is `prim::halo` then `prim::conv2d`. A conv
  reshards its input only with `reshard_if_not_optimal` or a failed shard check, and adds a
  `prim::move` when `deallocate_activation` is set. `reallocate_halo_output` defaults to true and
  adds a `prim::move` after halo; the move is skipped when the new buffer lands at the same
  address, so mark it `unverified`.
- `max_pool2d` defaults to bf16 ROW_MAJOR output; a following 1x1 conv then pays a tilize.
- `avg_pool2d` takes the global fast path only when the input allows it; otherwise halo + pool2d.
- Count totals by stage in a scratch list, then sum once. Do not add up by hand across messages.

## Subagent contract (larger models)

Give each subagent one block (stem, one layer type, head), the pinned SHA, the profiles, and this
file. It returns only CSV rows in the `op_table.csv` and `call_trace.csv` schemas from
`<plugin-root>/references/schemas.md`, with `confidence` set honestly. No prose summary.
