# Prefill and deterministic prefill (goal 5)

Once decode is fast, time to first token becomes the visible latency, and prefill runs a
different shape of work: tall activations, compute- and dispatch-bound chunks. This goal makes
prefill faster without changing its output, and adds an opt-in mode that makes prefill
bit-reproducible run to run.

## Bit-identical speedups

Every change here keeps prefill logits bit-identical to the previous build; prove it with output
digests at several prompt lengths on every mesh, not with PCC.

- **Fuse the expert epilogue.** On the reference model the MoE activation (two slices and two
  multiplies) became one `generic_op` that is 2.3× faster per expert group and spends 42 µs of host
  time per call instead of 80-109 µs. To stay bit-identical it reproduces the element-wise op's
  numerics, including its 16-bit destination registers; a "more accurate" fused kernel is a
  numerics change and needs the accuracy gates instead.
- **Pad small groups.** Padding each expert group of a short chunk to a fixed row count turned a
  ragged set of programs into one cached program per width (a 1k-token warm prefill went
  144 → 133 ms). Calibrate the pad per mesh; on two chips it did not pay.
- **Keep host routing off the critical path.** The host step that groups tokens by expert used
  OpenMP threads; on a loaded host the threads stalled and served 4k prefills showed a 60-80 ms
  tail. Running it single-threaded up to 8,192 rows removed the tail and cost about 3 ms on a quiet
  host.
- **Overlap and asynchronous reads** where the prefill path waits on the host.
- **Warm every prefill bucket at boot,** before the first decode capture ($tt-enable-tracing). A
  new prefill program compiled after capture is the defect the trace rules exist for.

Reference result on four chips: 4k-token time to first token 459 → 392 ms, 1k 164 → 144 ms; on
one chip 4k 818 → 617 ms; 128-token unchanged.

## Opt-in deterministic prefill

Check whether prefill is reproducible: run the same prompt three times on a fresh server and
compare logits bit for bit. On the reference model's four-chip path the attention all-reduce
(an asynchronous reduce-scatter followed by an asynchronous all-gather) left device 0's copy stale
in a few tiles, so 128-token prefills gave three different results (even the first argmax
differed); 1k and 4k prompts happened to be stable.

- Offer the stock one-op `ttnn.all_reduce` for that call behind a switch. It made every prefill
  bit-identical at no measurable cost on the reference model (167.2 vs 167.0 ms at 128 tokens);
  measure the cost on your model at every length, because it can differ.
- Keep it opt-in until the asynchronous pair is fixed upstream; it is a correctness property some
  users need (evaluation, debugging, reproducible output) and others do not pay attention to.
- Test it: three prefills with the switch must give identical logits and identical device replicas
  after every attention all-reduce; without the switch, record what happens.
- A bit-exact test elsewhere (for example a prefill-after-decode-trace check) fails on the default
  path for this reason alone. Run such tests with the switch on and say so.

Upstream draft for the reference model: tenstorrent/tt-metal #61226.

## Record

Time to first token before/after at 128, 1k and 4k (and the context contract's long length) on
each served mesh through the served harness; prefill digests before/after; the deterministic mode's
status, three-run results with and without it, and its cost. Write `doc/fast_path/prefill.json`
([validation](validation.md#goal-5-prefill)).
