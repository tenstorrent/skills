# Prefix caching and batched prefill

Every text model that completes the vLLM stages serves with vLLM automatic prefix caching (APC) on and prefills several admitted requests in one device pass. Both are model work. The plugin already schedules them; the model must execute them correctly and prove it. This document holds the contract, the tests, the thresholds, and the two documented exclusions.

Measured context: a 16K shared prefix cut TTFT from 24.8 s to 179 ms on one chip; a 32-request burst of short prompts cut TTFT from 6.3 s to 3.8 s on another model. The same features corrupt output when the resume path is wrong: one shipped model returns garbage for any repeated prefix over about 130 tokens. The tests below exist to catch that class before release.

## What the plugin sends

`TTModelRunner.submit_prefill` makes one `prefill_forward` call per prefill step with every admitted row:

| Keyword | Meaning |
|---|---|
| `tokens` | `[B, T]` token history from position 0 through the largest chunk end. |
| `start_pos` | Absolute chunk start per row. Nonzero for a prefix-cache hit (`num_computed_tokens`), a chunked continuation, and a preemption resume. |
| `prompt_lens` | Absolute chunk end per row. `prompt_lens[i]` is not the chunk length. |
| `page_table`, `page_tables_per_layer`, `kv_cache` | vLLM-owned block addresses and the cache the adapter allocated. |
| `empty_slots` | Destination device-state slot per row, when the model keeps per-slot state. |

Row `i` computes `tokens[i, start_pos[i]:prompt_lens[i]]`. A step is all-prefill or all-decode. The vLLM hybrid KV coordinator aligns a cache hit to the least common multiple of the model's block sizes and caps it at `prompt_len - 1`; chunked and resumed starts can be any value. Read the pinned plugin's execution and scheduling documents (currently `docs/MODEL_EXECUTION.md` and `docs/SCHEDULING.md` on `main`; an older pin may hold the same contract in `docs/SCHEDULING.md` and the model runner source) for the current wording.

## Continuation invariants

A model that satisfies these serves prefix-cache hits, chunked continuations, and preemption resumes through one path.

1. Treat `start_pos[i]` as the absolute start and `prompt_lens[i]` as the absolute end. Process only that slice. Use RoPE positions `arange(start, end)`.
2. Write K/V at absolute positions. Shift the paged fill view to the first page of the chunk. Write the unaligned leading rows and the non-tile tail token by token at their absolute index. Never write tile padding into live cache rows.
3. Full-attention layers write the chunk first, then read the whole prefix through the request's page table with chunked SDPA. Keep `chunk_start_idx` a multiple of the tile; front-pad Q by `start % tile` and slice the padding back out.
4. Sliding-window layers snapshot the last `window - 1` tokens from the cache before writing the chunk, because the new rows may overwrite them. Prepend the snapshot as K/V with zero Q rows and run windowed causal attention.
5. Keep page tables absolute and full width, `cdiv(max_model_len, block_size)` columns, for sliding-window groups. Do not apply a position modulo under vLLM; vLLM reclaims old pages itself.
6. Use the current host page-table snapshot on every call. Invalidate it when the device table is swapped or changed in place. Update device tables in place so decode traces stay valid.
7. Keep cache rows in execution-row order. For a model without recurrent state, `empty_slots` are not cache row ids.
8. Return one last-token output per row, in row order, taken at `logical_len - 1`. The runner host-samples intermediate chunks.
9. Accept any start position. Cache hits are block-aligned; chunking and resume are not.
10. For exact output after preemption, declare `supports_original_prompt_lens` if the pinned plugin documents it and replay already-generated tokens through decode instead of prefill.

Declare `model_capabilities["supports_prefix_caching"] = True` only after the tests below pass. Declare no other capability key for prefix caching unless the pinned plugin documents it.

## Exclusions

Set `supports_prefix_caching` to `False` and record `status: not_supported` in `doc/full_model/serving_contract.json` only for one of these, and name it in the row's `reason`:

- `block_output`: the model declares `output_tokens_per_step > 1`. The plugin rejects prefix caching with block output.
- `recurrent_state`: the model carries recurrent or linear-attention state (Mamba, Gated DeltaNet, conv windows) that vLLM-owned KV blocks cannot rebuild at `start_pos`. A hit would resume with stale or zero state.

Any other reason is unfinished model work, and `check_serving_contract.py` rejects it.

## Generator-level resume check

Implement `Generator.prefill_logits_continuation(prompt_token_ids, start, *, positions=None, prefill_prefix=True)` from the plugin's `readiness_check.contract`. It resets the generator-owned cache, prefills `tokens[:start]` as a fresh request, prefills `tokens[start:]` as a continuation at absolute position `start` over the same cache and page table, and returns `[1, len(positions), vocab]` logits. `start=0` is the reference. `prefill_prefix=False` skips the prefix prefill and is the negative control.

Run:

```bash
python -m readiness_check.run_prefill_resume_check \
  --model-dir models/autoports/<model> --mesh-device <mesh> \
  --lengths 2560,16384 --block-sizes <per-layer block sizes> --sliding-window <window or omit> \
  --output models/autoports/<model>/readiness/prefill_resume_check.json
```

The runner derives the resume starts from the geometry: the smallest block, the least common multiple of the blocks, a tile-unaligned start, the sliding window and one past it, the exact prefix-cache hit for a repeat (`floor((L-1)/lcm)*lcm`), and `L-1`. It compares full-prefill logits with continuation logits over `[s, s+256)` plus the last position. Prompts are deterministic natural text through the generator tokenizer: the readiness book in the target checkout (`models/tt_transformers/tests/tale-of-two-cities.txt.bz2`) or `--text-file`. The text must be long and non-repeating: a few repeated paragraphs make the suffix predictable without its prefix, and the controls stop failing. Random token ids are a stress variant (`--prompt-source random`), not the gate: their next-token distributions are near flat, argmax flips under ordinary BF16 drift, and top-1 agreement stops measuring the resume path. On a 30-layer MoE model a per-layer continuation PCC of 0.999 still produced top-1 agreement near 0.3 on random tokens, while natural text gave PCC 1.000 and top-1 1.000 at every start.

Gates per start: mean PCC >= 0.99, minimum PCC >= 0.98, top-1 agreement >= 0.98, final-position top-1 equal. Continuation changes MoE and SDPA chunk boundaries, so bit equality is not expected; BF16 drift at the margin is legitimate. A failure here is a cache address, position, or history bug, not drift, with two known exceptions. First, kernel selection by row count: a norm or matmul kernel chosen by chunk length makes the continuation of N rows compute the prefix differently from the full prefill over the same rows, and the gate fails with a correct cache (one model lost `pcc_min` to 0.876 this way). The prefill path must select kernels so both computations agree, or the difference must be shown inside the gates; check this before suspecting the cache. Second, the exact prefix-cache-hit start for a repeat leaves a window of at most one block (32 positions at block 32), where a single BF16 near-tie argmax flip is a top-1 of 0.969. The runner records the thresholds it used in the output; the gate reports a pass under lowered thresholds as advisory. Keep the default-threshold run beside any lowered one, with per-position margin probes that show the flip is a near tie and the fresh path agrees with HF at that position.

Controls: `no_history` (continuation over an empty cache) and `fresh_suffix` (the suffix prefilled at position 0, through the same driver so only the window's logits are returned) must miss at least one of the gates above wherever the window has at least 64 positions; on natural text they typically score PCC mean 0.65 to 0.82 and top-1 0.5 to 0.77, which top-1 alone would not always reject. Return only the requested positions from `prefill_logits_continuation`: all-position logits for a 16K prompt do not fit, so the driver selects rows before the final norm and LM head. A control that still agrees means the check cannot see a broken resume; fix the check before trusting a pass. For a sliding-window model, add one model-local control that zeroes the history snapshot; it must break the sliding layers.

## Served prefix-cache check

Launch the server with `--enable-prefix-caching` through the readiness runner, or pass the flag in the hand-launched command and point the runner at the log with `--server-log`. Then:

```bash
python -m readiness_check.run_vllm_server --stages prefix_cache \
  --server-url http://localhost:<port> --model-dir models/autoports/<model> --hf-model <id> \
  --server-log models/autoports/<model>/readiness_vllm/server.log
```

An identical repeat resumes one token before the end of the prompt, so the warm requests below prove the hit path and the timing, not a long continuation. The stage therefore also appends a 256-token new suffix to the shared prefix (a multi-turn shape), serves it once from the cache and once cold after a reset, and compares the two. That long-resume comparison needs `POST /reset_prefix_cache`, which vLLM exposes only with `VLLM_SERVER_DEV_MODE=1` in the server environment; set it for readiness servers. Without it the stage records `long_resume.checked = false`, the gate reports an advisory, and the generator-level resume check above is the only long-continuation proof.

The stage resets the prefix cache, sends one deterministic prompt of 2305 tokens cold, warm, warm, resets again, then cold and warm again, then the long-resume pair. Around every request it reads `vllm:prefix_cache_queries` and `vllm:prefix_cache_hits` from `/metrics`, and it records TTFT from the first streamed chunk, the first token and its logprob, and 64 greedy tokens. It also runs the shared-prefix `vllm bench serve` workload (4 prefixes of 2048 tokens, 128-token suffixes, 32 requests at concurrency 8) and records its hit rate. It writes `readiness_vllm/prefix_cache_check.json`; every run also writes `readiness_vllm/vllm_run_summary.json` with the server command, the flag, and the prefix-caching state read from the log. The prompt length is one token past a 256 multiple so a repeat hits every full block for block sizes up to 256; with a larger block or lcm, pass `--prefix-cache-prompt-tokens` as `k * lcm + 1`.

Verdict:

| Gate | Threshold |
|---|---|
| Server log | a "disabled" line fails; no log available is advisory, so pass `--server-log` |
| Warm request token hit rate | >= 0.9 |
| Cold request token hit rate | <= 0.05 |
| Median warm TTFT / cold TTFT | <= 0.5 |
| First token | identical to cold |
| First-token logprob delta | <= 0.25 nats |
| Greedy output | identical through 16 tokens; divergence index after that is recorded, not failed |
| Shared-prefix hit rate | >= 0.7 |
| Long resume (when reset is available) | resumed request hit rate > 0.5, cold reference hit rate <= 0.05, first token identical, first-token logprob delta <= 0.25 nats, greedy identical through 16 tokens |

Accuracy fence at this stage: the `sampling` and `qualitative` stages run against the same caching-enabled server, so their verdicts are the fence here; record the prefix-caching state beside them. Full-output greedy equality is not required; the resumed chunk starts at the cached length, which moves the chunk grid, and BF16 logits differ at the margin. A fixed-subset lm-eval comparison with caching on against the accepted caching-off result belongs to the benchmark stage and is not required to pass this gate.

Where the pinned plugin offers `additional_config.tt.prefill_chunk_alignment`, do not use it to chase cache-hit numerics; it rounds partial chunk ends only and does not change where a hit resumes. It is useful for shape stability of chunked continuations, which keeps first-encounter programs, and the decode-trace releases they cause, bounded.

### Sliding-window models under the pinned plugin

Two plugin behaviors can block a sliding-window model that meets the contract above. Both are plugin-owned under [plugin changes](plugin-changes.md), not reasons to declare the capability `False`:

- Some plugin revisions switch automatic prefix caching off for any model whose config has a sliding window, and log "Automatic prefix caching is disabled". That is a default-behavior change for a model that declares and proves the capability: case 1 when the plugin's own capability document says the declaration permits caching, case 2 otherwise. Removing the veto changes plugin default behavior, so it needs human authorization.
- The runner trims the block table to `min(cdiv(max_model_len, block_size), num_blocks)`. A sliding-window continuation needs the full absolute width, or a late chunk fails with a page table that does not cover its history. This is case 1.

For either, record the row as `plugin_owned` in the serving contract, write the justification record with the reproducer, the model-side alternative and why it is wrong, the effect on other models, and the regression test, and follow the authorization rules. The Stage 9 gate reports a `plugin_owned` row without served evidence as advisory, so the stage ends `blocked` on the authorization rather than failed. Do not solve either with a model-specific plugin branch or an undocumented capability key.

## Batched prefill

Several admitted requests arrive in one `prefill_forward` call. A per-row loop over the full decoder is correct but reads every weight once per row; short-prompt bursts then pay B times the weight traffic. Batched prefill runs the dense work once for all rows.

Design, in order of simplicity:

1. Equal padded length. Group rows whose padded length is equal and run them as one `[B, S]` batch: QKV, norms, MLP or MoE, and the LM head on `[1, 1, B*S, H]`; per-row paged fill and attention with a `[B, H, S, D]` SDPA. This is the shipped `tt_transformers` and gpt-oss pattern.
2. Packed rows. Concatenate rows of different lengths along the sequence axis into a bucket (1024, 2048, 4096 tokens), pad each segment to a fixed grid (128), run dense work once, and loop over segments only inside attention and cache fill. Gather each segment's last hidden row before the final norm and LM head.

Rules for either design:

- Pack only rows with `start_pos == 0` and padded length at or below the configured maximum. Rows with nonzero `start_pos` and long rows run on the single-row path in the same call. Preserve row order in the output.
- Padded keys sit after the real tokens of their segment so causal attention never reads them; padded queries are discarded. Padding writes, where a padded-residual path cannot avoid them, stay inside the row's own pages up to its block-aligned length and never touch another row's pages; decode overwrites them before any read.
- Hybrid models with recurrent or convolution state: that state does not live in vLLM pages. Run the recurrent kernel per row (or on small sub-batches) from a shared zero state, then scatter each row's final state and convolution tail straight into its decode slot. FP32 state cannot go through a BF16 gather, so scatter it with per-slot writes warmed for every slot. After a multi-row admission the plugin remaps slots; on one model that remap cost 1.05 s per burst and dominated TTFT while the generator-level calibration showed a full win. Measure at the adapter boundary (a per-step timeline of prefill, remap and first decode) before and after, not only in the generator.
- RoPE positions are per segment, `arange(0, len_i)`; padding repeats the last real position.
- Warm every bucket and segment grid at generator preparation time, before the first trace capture, and every single-row length the acceptance workload will send: the allocation tracker reports the first compile of an eager path as a live buffer, so an unwarmed length fails a tracked run for the wrong reason. Record `num_program_cache_entries()` before and after warmup and assert zero growth during the acceptance workload. A new prefill program after warmup releases decode traces on many generators; that is the dominant cost of this feature.
- Keep program keys independent of row position and logical length. These are compile-time program keys and each distinct value compiles a new program: `ttnn.slice` start indices, `ttnn.pad` amounts, `ttnn.embedding` on an ids tensor shaped by the logical length, chunk-local page-table slices, per-slot `slice_write` offsets, per-source-slot remap slices, a scalar `batch_idx` to `paged_fill_cache`, and sampler traces keyed by parameter values. A loop over rows or an unseen length then compiles programs after warmup, and on most generators that releases the decode traces (on one model every unseen prompt length cost 8 to 50 programs and a 1 to 8 s recapture). Use host-padded fixed row buckets (2, 4, 8, 16, 32 rows, dummy rows repeating the last real row), runtime-index gathers (`ttnn.embedding` on a fixed-shape index tensor; BF16 only) for row extraction, RoPE rows, tails and last-row selection, a tensor `batch_idx`, a fixed-width fill page table, a tensor `chunk_start_idx`, and sampler trace keys that name the graph (penalties on or off, logprob mode) rather than the values. `ttnn.split` does not help: in current tt-metal it is a per-offset `ttnn.slice` and keys the same way. Prove it with the program-cache count across every bucket, every slot, slot compaction, and the sampler variants before the first capture.
- Size the burst to the scheduler's per-step token budget. A prompt that overflows `max_num_batched_tokens` is split into chunks, the plugin host-samples the intermediate chunks, and the generator releases decode traces; a burst measured that way attributes scheduler behavior to the model. Set the budget to cover the burst (32 x 128 needs at least 4096) or record the chunking in the result.
- Give each experiment series its own tensor-cache directory. A cache entry truncated by a process killed mid-write stalls every later loader on the host (one thread at full CPU, no I/O, no log line) and looks like a hang.
- Set `TT_VISIBLE_DEVICES` on host-only test sweeps too: a parametrized device fixture in an unrelated test module opens every chip on the host, including chips another job holds.
- The packaged runner's `serve` stage writes `server.log` and the benchmark files to the fixed `readiness_vllm/` root, so a serial-reference server on a second chip cannot run through it at the same time; launch that server by hand with the same command, attach with `--server-url` and `--server-log`, and copy its summary to the serial-reference name before the batched run.
- Calibrate first: time single-row prefill at the bucket sizes and keep only buckets where one packed pass is faster than the sum of the rows it replaces. On device-bound MoE prefill the gain at 32 x 128 is about 2.5x to 3x, not the 7x seen on host-bound dense models.
- Default on only after the gates below pass. Keep an environment override for A/B measurement and record its name.

### Acceptance

Batched equals solo, generator level: rows of lengths `{5, 31, 32, 33, 127, 128, 129, 257, 640, 1000, 1024}` in two orders, B in `{2, 4, 8, 32}`, random-token prompts plus a few real chat prompts. For every row, compare the packed result with the single-row result: top-1 equal for 100 percent of rows; PCC on last-token logits >= 0.999 per row (TT against TT, only accumulation order differs); K/V pages for one layer of each kind equal with zero tolerance at the real positions (read whole cache tensors back to host and index there; per-block device slices compile per-offset programs and release traces); recurrent and convolution state rows equal where the model has them; a 32-token greedy continuation with the same decode batch in both arms identical through 8 tokens for every row (later divergence is recorded).

Burst TTFT, serving level:

```bash
python -m readiness_check.run_vllm_server --stages prefill_burst \
  --server-url http://localhost:<port> --model-dir models/autoports/<model> --hf-model <id> \
  --prefill-burst-serial-result <serial summary json>
```

Run the stage once with batched prefill disabled through the environment override and copy the resulting `vllm_prefill_burst_benchmark.json` to a distinct name such as `vllm_prefill_burst_serial_benchmark.json`, then run it with batched prefill on and pass the copy. The stage reads the serial file before it runs, so passing the stage's own output path is accepted, but keep the copy as evidence. The stage records `serial_reference.config_matches`; a serial run with a different workload shape does not count. Gate: p99 TTFT ratio <= 0.6 and mean ratio <= 0.5. Stage 9 reports this as advisory; Stage 10 requires it.

Single-user unchanged: the primary 128/128/1 TTFT p50 and TPOT mean stay within 5 percent of the pre-change `vllm_benchmark.json`, and the mixed-shape trace reuse check shows zero recaptures after warmup with the batched path active.

Stage split: Stage 6 proves the multi-row contract (one call with mixed rows equals the solo result per row); Stage 7 keeps it and calibrates bucket sizes without implementing the batched path. Stage 9 measures the burst on whatever prefill path exists and records it; when prefill is still one row at a time, that measurement is the serial reference and the ratio gate is advisory. Stage 10 implements batched prefill, produces the batched-equals-solo evidence, and must meet the ratio gate.

## Gate

`scripts/check_prefill_features.py` runs at the end of the Stage 9 and Stage 10 check scripts. It reads the serving contract row, `vllm_run_summary.json`, `prefix_cache_check.json`, `readiness/prefill_resume_check.json`, and `vllm_prefill_burst_benchmark.json`, all at the `readiness_vllm/` and `readiness/` roots of the model directory; profile subdirectories are not read, so run the stages against the profile that the stage reports. Critical: a supported claim without passing artifacts, a disabled-APC server log, a negative control that did not fail, an undocumented exclusion, or, in Stage 10, a missing or failed burst comparison. Stage review adds the judgment the gate cannot make: whether the evidence covers the model's layer kinds, and whether a divergence recorded as drift was investigated.

## Evidence to leave

For an excluded model (`block_output` or `recurrent_state`): the serving contract row naming the exclusion, and the batched-prefill evidence only. The resume check and the `prefix_cache` stage do not apply, and the gate reports their absence as advisory.

- Serving contract rows for `supports_prefix_caching` and batched prefill with the artifact paths.
- `readiness/prefill_resume_check.json` with controls, and the model-local sliding-window control when applicable.
- `readiness_vllm/vllm_run_summary.json`, `readiness_vllm/prefix_cache_check.json`, the shared-prefix benchmark files, and the sampling and qualitative verdicts recorded against the caching-enabled server.
- `readiness_vllm/vllm_prefill_burst_benchmark.json` with `serial_reference`, the batched-equals-solo test output, the warmup bucket list, and the program-cache entry counts before and after the workload.
- Before and after primary single-user metrics.
