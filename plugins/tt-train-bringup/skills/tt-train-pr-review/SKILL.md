---
name: tt-train-pr-review
description: Strict pre-merge review of tenstorrent/tt-metal PRs that touch tt-train/ (ttml::metal kernels, autograd ops, modules, tests, benchmarks, multi-device). Use when preparing a tt-train PR for review, when reviewing one, or when deciding whether an optimisation is worth doing. Encodes the tt-train team's kernel perf patterns, recurring bug classes, test/benchmark/CI conventions, and a multi-agent adversarial review process.
---

# tt-train PR review

This skill is distilled from the tt-train fused kernels and about 1,000 review comments by the tt-train team: @mdragulaTT, @philei-tt, @zbaczewskiTT, @dyurshevichTT, @vmelnykovTT, @ayerofieiev-tt, @lgalasTT, @nmauriceTT and others.

- **Scope.** It complements the generic `tenstorrent/skills` reviewers, which cover CB races, L1, precision and CCL (see `.github/workflows/tenstorrent-skills-reviewer.md`). This skill covers only what is specific to tt-train.
- **Paths.** They are relative to `tt-train/sources/ttml/`. `#N` is a tt-metal PR you can read as precedent.
- **Hardware.** tt-train is Blackhole-only, so never ask for Wormhole runs or argue from Wormhole limits.

## 1. Process

1. **Pin the head.** Get it with `gh api repos/tenstorrent/tt-metal/pulls/N --jq .head.sha`, read files at that SHA, and diff against the merge-base. Re-fetch right before you report, because authors force-push.
2. **Fan out to 2–4 independent reviewer agents.** Give each one the diff and this file, plus one lens:
   - **A: kernels.** Dataflow and overlap, DST/CB/L1 budgets, bug classes (§4, §5).
   - **B: host side.** Device op, validation, hashing, autograd, placement, duplication (§3, §6, §10).
   - **C: evidence.** Tests, benchmarks, CI runs and the PR description (§7–§9).
   - **D: numerics and distributed correctness.** Only for big or multi-device PRs (§10).

   Hand them the code, not your conclusions.
3. **Unify.** Merge duplicates and keep the strongest evidence for each. Drop anything that rests on a hunch with no evidence.
4. **Verify adversarially.** Give each slice of findings to a fresh skeptic agent whose only job is to *refute* them against the head. It re-runs the numbers, runs the test, re-reads the line and checks what main already does. Fix or drop whatever fails.
   - Never argue from an estimated magnitude; run it.
   - First drafts are routinely wrong: one strict pass refuted 3 of 41 comments and corrected 27 more.
5. **Check anchors.** Inline comments must sit on lines inside the PR patch (`gh api --paginate repos/tenstorrent/tt-metal/pulls/N/files`). GitHub rejects anchors on pure renames and on unchanged context.

## 2. Is it worth it? Ask this first

- **Step gain = share of step × achievable speedup.** Profile the consumer before touching a kernel. In Tracy, use `DEVICE KERNEL DURATION`, not FW duration.
- **Profile at four doubling block depths, never the full depth.** Tracy over every layer of an LLM is too expensive. A block is the smallest repeating group of layer kinds (one layer for a homogeneous model; 3 linear-attention + 1 full-attention for Qwen3-Next, where two bare layers are not representative). Build the model at 2, 4, 8 and 16 blocks (1, 2, 4 and 8 when a block is several layers) with real shapes, profile each, fit a line (per-block cost from the slope, fixed cost from the intercept) over the four points, and extrapolate to the full layer count. Quote the share of step from the projection and say so. Every point must sit on the line within noise; if one does not, the step has a term that does not scale with depth and the projection is not evidence. See the `optimization-loop` skill's measurement protocol in this plugin.
- **An op at about 1–2% of the step** gets cheap structural wins only; micro-tuning isn't worth it. Sub-0.1% wins are below what a benchmark can resolve.
- **Low-hanging fruit is often outside the kernel:**
  - Host dispatch: composite Muon issued about 4,000 dispatches, roughly 700 ms per step (#37127).
  - CCL composite fallbacks: removing a reduce-scatter fallback took TP+DDP MFU from 7.8% to 15.2% (#42675).
  - Gradients computed for frozen weights.
  - untilize/tilize round trips.
  - Per-forward host uploads or `to_vector` syncs.
  - `ttnn::sum` over size-1 dims, which writes full fp32 copies (249 → 34 µs, #62511).
- **Fuse the data-movement glue, not the matmul.**
  - Full SwiGLU fusion lost to ttnn matmul at real shapes. The fused forward was 22% faster per block on NanoLlama3 but 3.9× slower per block on TinyLlama, 1.33× slower per step (#34172, #38767). What shipped is ttnn matmuls plus a fused elementwise backward (#39969).
  - Pure data-movement fusions win big. MLA kv-assemble beat the slice/concat/permute chain by 22–27× in forward and 20–26× in backward (N300; #45541, #45621). `mla_q_rope` is 2.7–6.5× faster (#46188).
  - For a bad matmul shape, reshape the problem so one ttnn matmul gets a good shape.
- **Fix the autograd graph before writing a kernel.** Two `slice` backwards (each allocating full-width zeros + concat, then one add) became one `split` whose backward is a single concat. The split backward got 5–12× faster in isolation (N300); the whole layer backward gained only 1.04–1.07× (#46679). Report both levels.
- **Backward kernels usually pay more than forward ones.** RMSNorm: forward −5.5% step, backward −9.2%, both −14.7% (#22828). Measure fw-only, bw-only and both.

## 3. Placement and reuse

- **Placement.** Training ops live in `metal/ops/<op>/` (`ttml::metal`), never as new ttnn API. Calling existing ttnn ops from ttml is fine.
- **Extend, don't duplicate.** A kernel that duplicates a ttml kernel becomes a mode or define of it. Gated and distributed RMSNorm went into `rmsnorm_fw/bw`; the silu' math belongs in `swiglu_gate_bw_block`.
- **Must-reuse helpers.** A hand-rolled copy of any of these is a finding:

| Header (`metal/common/`) | Use it for |
|---|---|
| `compute_utils.hpp` | `pack_and_push[_block]`, `pack_l1_acc_block`, `pack_{two,three,four}_l1_acc_tiles`, `zero_dst_reg` |
| `dataflow_utils.hpp` | `read/write_tiles_by_row<UseBarrier>`, `read_full_row_tiles`, `read_tile_block_transposed`, `fill_zeros_async`, the mask/scaler tile generators, `get_sem_ptr`, `mcast_sender_signal_receivers_loopback`, `advance_q_block`, the `*_ONE_BITS` constants |
| `program_utils.hpp` | `create_circular_buffer`, `create_{reader,writer,compute}_kernel`, `get_block_size`, `for_each_core_with_work` (in `create`), `for_each_core` (in override) |
| `tensor_validation.hpp` | `check_device_tensor(t, op, name, {.buffer_type = DRAM, ...})`, `check_same_device` |
| `sdpa_compute_utils_common.hpp`, `first_column_compute_utils.hpp` | `sdpa_exp_tile` (scale folded in), first-column-only SFPU on reduced tiles |
| `test_utils/` | `make_uniform_xarray`, `relative_l2`, `mesh_utils` fixtures |
| `tests/benchmark/benchmark_utils.hpp` | `time_device_avg_us`, `capture_dram_peak` |

- **Shared definitions.** Layout math and constants live in one header that both host and kernels include. Forward and backward share a `*_common.hpp`.

## 4. Kernel perf checklist

**Dataflow and overlap**
- **CB depth.**
  - Streaming CBs are 2×block, so the reader fetches block n+1 while compute works on block n.
  - Compute-only intermediates don't need double buffering.
  - Output CBs drained per block need 2×block, not Wt.
  - For bandwidth-bound ops, measure: AdamW gained 19–33% GB/s on large shapes from a 1× in/out depth (#39951).
- **Barriers.** Issue all of a block's NoC reads, then one barrier. A barrier per tile or per row serializes the reader.
- **Constant tiles** (scaler, eps, mask) are generated once per core and stay at the CB front. Generate them on the writer so the reader starts DRAM reads at once (sdpa_fw).
- **Pass-through data** goes reader → writer and never through DST (`mla_q_rope` q_nope).
- **NoC split.** Activations and outputs go on one NOC, weights on the other (#34172).
- **Zeroing.** Zero memory with NoC zero-writes (`fill_zeros_async` / `async_write_zeros`, as in moe_group), not RISC store loops. Pair them with `write_zeros_l1_barrier`, not a read barrier (#53220).
- **Avoid re-reads.**
  - Provide L1-fit modes: everything fits, everything except gamma, or streaming (rmsnorm_fw).
  - Read gamma once per core.
  - Compute once and share: sdpa_bw computes `u = rowsum(dO·O)` in the Q kernel, so the KV kernel stops reading O (−1.4% step, #39812).
  - For data-bound ops, count DRAM passes. One pass accumulating several sums beats several passes (PolyNorm, #40224, #40449). Report % of DRAM roofline.
- **Uneven multicast splits.** Every core loops `max_rows` times; cores with less work drain their CBs without writing (#34172).

**DST and compute**
- **DST budget.** Half-sync with `fp32_dest_acc_en` gives 4 tiles (8 in bf16). `block_size` = budget − scratch registers.
  - Derive it from the DST size: `dst_size = fp32_dest_acc_en ? 4U : 8U; get_block_size(Wt, dst_size - 1U)` (sdpa_fw program factory). Assert it on the host: the `Tr + 1U <= max_dst_tiles` TT_FATAL in mla_kv_assemble_bw (#53221). Don't scatter hard-coded 3s and `4U`s.
  - `get_block_size` returns 1 whenever Wt has no divisor in {2, 3, 4} (e.g. 5, 25, 35). Consider a fixed block size with a tail.
- **Accumulation.** Accumulate in DEST across tiles and pack once. Otherwise use packer L1 accumulation (`pack_l1_acc_block`, `pack_reconfig_l1_acc`): in SDPA that freed 4 CBs and cut the step 4% (#36355).
- **No pack → L1 → unpack round trips** when DEST can be reused: use `mul_reuse_dest` / `add_reuse_dest`, or an in-DST `transpose_dest` (sdpa_bw KV kernel −2.4%, #53315).
- **Inits and reconfigs.**
  - Hoist `*_init()` out of per-tile loops.
  - Use the 2-argument `reconfig_data_format(old, new)` and `pack_reconfig_data_format(old, new)`; on one PR, 8 of 10 reconfigs changed nothing.
  - Re-issue `mm_init_short` after SFPU inits (#44198).
- **Reduced tiles.** Run SFPU on the first column only (`VectorMode::C`: 8 iterations, not 32) and fold the scale into exp. Running sqrt, recip or rsqrt on a full REDUCE_ROW tile is waste. Together with heavy-row-first scheduling, these gave −22% on the sdpa_fw kernel (N300, #42820).
- **Cheaper math** (#42968, #40449):
  - Horner form.
  - Fold the weight into inv_rms once per row.
  - `add_unary_tile` for eps instead of an eps tile.
  - Compute a shared subexpression (e.g. sigmoid in silu') once and reuse it.
- **Precision.**
  - Keep sums in fp32 CBs. Row-reduce by matmul with a ones tile.
  - fp32 through SrcA truncates to TF32, so use `UnpackToDestFp32` for fp32 elementwise ops (#53315).
  - fp32 intermediates are not automatically better; measure (#25263).

**Precedents**
- **SDPA:**
  - Online softmax.
  - One fp32 LSE tile saved for backward (#41683).
  - The causal mask tile is generated once on device, and fully-masked blocks are skipped (no O(S²) mask).
  - Heavy/light row pairing: −33% step vs the unbalanced fused kernel (#37457).
  - K/V chunk size bounded by DST (#44198).
  - Open ideas are in `metal/ops/SDPA_OPTIMIZATION_PROPOSALS.md` (its numbers are from Wormhole).
- **MoE:**
  - `variable_matmul` takes M/K at runtime and reads offsets on device, so one cached program serves every expert size with no slice/concat (−10 to −53%, #44263).
  - Routing uses scatter + topk instead of loops over experts: 8× (#42859).

## 5. Kernel bug classes: check every one

1. **CB contract.**
   - wait/pop counts that don't match the tiles indexed (it only worked at Wt=1).
   - A pack without a reserve, or a missing push.
   - A CB sized to `block_size` that hangs at other sizes. Tests must force each block size (#44511).
   - An in-place operation on a 1-tile CB.
   - One CB index shared by two kernels.
2. **DST.**
   - Not zeroed before accumulating: `mul_tiles` accumulates, which caused a double subtract (#53215).
   - A nested `tile_regs_acquire` inside an accumulation window.
   - `block_size` larger than the DST budget (#53221).
3. **Init clobbering.** SFPU inits break matmul state, and the `transpose_dest` init clobbers the replay buffer.
4. **Optional inputs.** An unconditional wait/pop on a CB the host never created (a hang with `mask=None`, #53217).
5. **Padded vs logical.**
   - A scaler computed from the padded dim (1/8 instead of 1/√40).
   - Mixed row counts writing out of bounds.
   - Validation that compares only padded shapes.
   - Nonzero padding lanes leaking into grads (#53217–#53222). `to_xtensor` hides padding, so assert it separately.
6. **NoC.**
   - Fixed-size reads over short row-major pages.
   - A staging buffer reused while a write is in flight.
   - Strides or staging slots sized from raw sizes instead of the aligned page size. A raw `h*2` bank stride (#53220); 32 B slots against Blackhole's 64 B aligned DRAM page, which overran the staging CB (#62090).
   - No `noc_async_atomic_barrier()` after a semaphore increment.
7. **Multi-core protocol** (#53223).
   - Partner cores iterating sub-blocks in different orders.
   - Partial pushes into a CB whose capacity they don't divide.
   - No backpressure ack.
   - Multicast offsets reset while receiver pointers persist.
8. **Empty work.** K=0 leaves garbage in DST, and slack rows of shared outputs are never zeroed (#44263).
9. **L1 budget** computed separately from the CB plan drifts from it: rmsnorm_bw's fit check said "fits" and then ran out of L1 at Wt≈130–160 (Wormhole figures, #49389). Derive both from one CB plan.
10. **Distributed math.** Ring ops using per-chunk instead of global statistics (#53216); modular index underflow.

Run any CB or NoC change once with the watcher (`TT_METAL_WATCHER=<s>`). Some bugs only the watcher reports (#62090).

## 6. Host side: device op, factory, autograd

- **Validation** goes in `validate_on_program_cache_miss`.
  - Use `check_device_tensor` and `check_same_device` with op-prefixed messages.
  - Never return garbage or silently ignore an argument; an MLA mask was silently ignored (#45140).
  - No ttml op defines `validate_on_program_cache_hit`, so the miss validator also runs on every hit. Keep it cheap. Checks placed in `create()` run only on a miss, so they don't belong there.
- **Program hash.** Hash the whole attribute struct plus each input's dtype, shape and memory config: `hash_operation<Op>(...)`. `mla_q_rope_device_operation.cpp` is the precedent: `TensorAccessorArgs` bake placement into compile-time args, so every input's memory config must be in the key.
  - Fields that were missed in the past: weight shapes, other inputs, buffer type (an L1 tensor reused the DRAM program), `cluster_axis`, and `nullopt` hashed the same as 0.
  - Don't hash runtime args.
- **Overrides.** `override_runtime_arguments` must patch every buffer address. The cache-hit test (§7) proves it.
- **API shape.**
  - Name structs `<Op>Params` / `<Op>Inputs`.
  - Order prim params inputs → attributes → preallocated outputs.
  - Use enums, not bools, in C++; bool kwargs are fine in Python.
- **`preallocated_*` outputs are the house pattern;** never call them dead API.
  - Use them to reuse dead buffers. `ops/swiglu_op.cpp` (the swiglu backward) passes the saved `linear1` as `preallocated_dL_dlinear1`. The elementwise kernel reads each tile before writing it, and `linear1` is dead afterwards. The trick was part of #39969, which as a whole cut DRAM peak by up to 15% at batch ≥16 and NanoLlama step time by 3%. The same trick is used in `moe_ffn_swiglu` and the SDPA gate.
  - It assumes a single backward, so guard `retain_graph`.
  - Never alias a buffer that `add_grad` might adopt: the `+` backward shares one gradient between both inputs (#59632).
- **Frozen weights.** Skip their grad work with a flag that is part of the hash; don't compute the grads and then drop them.
- **Saved state.**
  - Save the minimum for backward: `rms`, not gathered stats, and one LSE tile.
  - Save it only when a backward node will exist, and free it at last use.
  - No host round trips in forward.
- **Defaults and bindings.**
  - The fused op is the default; keep `<op>_composite` as the fallback, with no runtime flag (#38664).
  - Don't ship slower fused variants behind an enum, because callers will pick the one named "full" (#38767).
  - A Python consumer needs the nanobind binding, either in this PR or as a linked issue.

## 7. Tests

- **Flow.** Each op gets `tests/ops/<op>_op_test.cpp`: `make_uniform_xarray` → `core::from_xtensor` → op → `core::to_xtensor` → `xt::allclose(a, b, rtol, atol)`. Note the argument order.
  - Don't use PCC; it is blind to scale and offset.
  - Use `relative_l2` for long reductions and `xt::equal` for pure data movement.
- **Independent reference** (xtensor, torch or fp64). Never build it from the op under test or from the loader's own helpers.
- **Tolerances measure the kernel, not input rounding.**
  - Round inputs to bf16 before computing the reference, and justify every bound.
  - Add a mean-error check instead of widening per-element atol.
  - Use magnitude-relative checks where error grows with sequence length.
  - Avoid vacuous passes: zero-mean inputs that make the output ≈0, or a NaN slipping through a `std::max` check.
- **Reach every path.**
  - More rows than cores, so both core groups run.
  - Non-aligned sizes, every block size, every mode or define, frozen mode.
  - Guards tested with `ThrowsMessage(HasSubstr(...))`.
  - Autograd tests go through `ops::` and `loss->backward()`.
- **Cache-hit test.**
  - Two live input sets with the same shape and different addresses: the second call must hit the cache (`num_program_cache_entries()` unchanged) and still be correct.
  - One case that must miss, e.g. a different buffer type or axis.
- **Fail before, pass after.** Run the new test once at the merge-base.
- **Cost.**
  - Use `TEST_P` tables with named rows. Prefix rows `NIGHTLY_` if they take more than about 5 s or are JIT-heavy.
  - Keep at least one blocking case per compiled variant, because a define is compiled only when some test runs it.
  - Multi-device C++ tests must not be `NIGHTLY_`: the QB2 leg filters them out.
  - Python device tests get `@pytest.mark.requires_device`.
  - Share helpers (e.g. `tests/python/bf16_ulp.py`) instead of copying fixtures.
- **Model-level changes** need loss parity over N steps, bit-exact for refactors. Plot it with `tt-train/scripts/plot_training_comparison.py`.

## 8. Benchmarks and the PR description

- **Harness.** `tests/benchmark/<op>_benchmark.cpp` on `benchmark_utils.hpp`. Sync once after the timed loop, not per iteration. No private timer copies.
- **Shapes** are the consumer's real per-chip shapes (after TP).
- **Baseline** is what the model runs today, i.e. the strongest composite. Time it at the autograd entry point the model calls. If you touch a shared kernel, also show before/after against main for its unchanged modes. Report the losses too.
- **Memory.**
  - Gradient checkpointing hides activation savings, so report the segment peak.
  - Free tensors at last use in both arms.
- **CI doesn't run benchmarks.** The numbers in the PR are the only proof.
- **The description becomes the squash commit.** It should contain:
  - Two sentences of motivation: profiled, X% of the step for this model/shape, the step-time or MFU gain.
  - Methodology: what is timed, warmup and iteration counts, where the sync is, the hardware.
  - A results table.
  - Tolerance rationale.
  - What is out of scope.
  - Behaviour changes to shared ops.
  - Verification: exact gtest filters and links to green runs at the final head.
  - Follow-ups as filed, linked issues.

## 9. CI evidence: "PR green" doesn't mean the kernel tests ran

- **PR push** runs lint, build and static checks only. "Verify changed tests" runs only the test legs the PR touches: the tt-train Python legs when `tt-train/tests/python` is edited, edited `tests/pipeline_reorg` entries, and entries whose command names a changed file. A code-only kernel change gets no device run.
- **C++ `ttml_tests -*NIGHTLY*`** run only in Merge Gate (the merge queue). To force a run:
  `gh workflow run merge-gate.yaml --repo tenstorrent/tt-metal --ref <branch>`
- **NIGHTLY C++ and all pytest** (including the 2-chip `bh_quietbox_2` legs) run only in L2 nightly. Dispatch `tt-metal-l2-nightly.yaml` with `run_tt_train_unit_tests=true run_blackhole=true run_wormhole=false`.
- **Approvals are sticky.** Ask for the green run at the final head before approving.

## 10. Non-kernel checks

- **Multi-device.**
  - Placement labels must match the data, and Replicate copies must actually be identical.
  - Compare the distributed result with the single-device op on the gathered tensor.
  - Single-chip CI skips mesh fixtures, so the PR needs a QB2 run.
  - A failure on any rank must end the job, not hang it.
  - Look for CCL composite fallbacks that move N× the bytes.
- **Optimizers.**
  - They are bandwidth-bound, so count bytes.
  - Restore state in the right order (beta powers after betas, #53222).
  - Use an empty grad instead of `zeros_like` (−16.6% peak DRAM without gradient accumulation, #43629).
  - Build derived precision copies lazily (#41385).
- **Numerics.**
  - fp32 → bf16 rounds to nearest even.
  - RNG output must not depend on thread count.
  - Fixed-seed tests catch what loss curves hide (`repeat` vs `repeat_interleave`, #43401).
- **Python and configs.**
  - Unknown YAML keys are silently ignored, so validate them.
  - Check HF/TRL parity claims step by step.
  - API changes keep a deprecated alias.
- **The repo is public.** No customer names and no internal links.

## 11. Code smells: cheap, always report

- **Duplication.**
  - A copy of a §3 helper, or the third private copy of anything.
  - Re-pasted `onetile`, mask or scaler snippets.
  - `0x3F80` instead of `BF16_ONE_BITS`.
  - A local `4U` DST constant.
- **Magic numbers.** Use `tt::constants::TILE_*`, named CB indices and `tt::round_up`. Use `if constexpr` where it works instead of `#ifdef`.
- **Dead code.**
  - Args or locals that are set but never used, and compile-time args that are voided.
  - Unused includes.
  - API with no caller (preallocated outputs excepted).
  - Wormhole-only branches.
- **Comments.**
  - Stale comments.
  - Fix history or review rationale in the code (it belongs in the PR).
  - Comment share far above about 15%.
  - Keep derivations.
  - A TODO needs an owner and an issue.
  - No DPRINT and no commented-out code.
- **One knob per decision.** A kwarg or an env var, not both. A kill switch is only for risky perf paths.

## 12. Output

- **Rank findings:** hang or crash > silent wrong result > missing CI evidence > perf with step-level impact > test gaps > hygiene.
- **Each finding gives** file:line at the head, the concrete failure (these inputs → this wrong output) or the number, and the fix or the precedent.
- **The top-level comment holds only** design or direction challenges and cross-cutting asks: motivation, a CI run, landing order with colliding PRs, issues to file. Otherwise it is a short LGTM. Never re-summarise the PR or the inline comments.
- **Tone.**
  - Strict content, friendly words: thank the author, write "Could you…" or "I'd suggest…", own your own earlier mistakes, offer to pair.
  - Post as COMMENT rather than REQUEST_CHANGES.
  - Authors answer each thread with "done" or "not done, because…" and resolve it.
- **External PRs** get a cheap correctness check and few nits, because every push needs a maintainer to re-approve fork CI.
