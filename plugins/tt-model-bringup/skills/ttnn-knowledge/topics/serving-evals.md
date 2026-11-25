# Serving and evals

The vLLM plugin and its setup, sampler routing, eval harnesses and their infrastructure, and slow benchmark and eval
runs.

## Plugin and vLLM

- **Top-k cap.** The shared device sampler handles top-k up to 32: the model's limit, the plugin's parameter
  formatting and the C++ op each cap it. A model whose recipe needs top-k 64 hits all three; raising only some of
  them reads other users' probabilities. The shared `ttnn.sampling` path draws from one 32-candidate tile per user
  whatever `k` says, so a declared `max_device_top_k` above 32 can emit token ids outside the candidates.
- **Checkpoint sampling defaults.** vLLM applies `generation_config.json` (`top_k`, `top_p`, `temperature`) to every
  request that does not override them, so a checkpoint default above the device ceiling silently routes the whole
  served recipe to the host sampler.
- **Per-step sampler routing.** The plugin chooses device or host sampling per step for the whole batch. A
  host-only parameter joining the batch (a `top_k` above `max_device_top_k`) moves every later step of that batch to
  the host and offsets the seeded streams of the other requests; seeded-reproducibility tests then fail without any
  model defect.
- **Counting host-sampled steps.** Neither the plugin nor the packaged runner logs them, and vLLM kills the engine
  on shutdown before adapter teardown prints; a per-step sampling-mode line logged during the run survives.
- **Penalties warmup.** The common sampler's `precompile` skips the output-count update, so a warmup through its
  API left the penalty scatter path uncompiled and the first penalties-on capture failed; one counted sampling pass
  before capture fixed it.
- **Async decode overlap.** vLLM can submit step N+1 before sampled token N reaches host scheduler state, and on a
  steady step the plugin's `start_pos` argument lagged the device position by one. The plugin overlaps decode only
  under vLLM `--async-scheduling`, which the packaged readiness runner does not pass, so a declared async split can
  go unexercised. Leaving overlap off was estimated to cost a few tokens/s per user.
- **`reset_batch=False`** did not prove the page table unchanged; what the plugin refreshes on a reset depends on
  its revision, and workarounds copied from an old fork did not hold.
- **Leftover engine processes.** `EngineCore` or `vllm.entrypoints` processes left after a crash held chip locks
  even after `tt-smi -r`.
- **Runner arguments.** `--additional-server-args` is split with `shlex`, so a JSON value inside it needs its own
  quoting.
- **Masked prefill rows** advanced the device sampler's RNG state in one plugin version.
- **Plugin changes need authorization.** Several bringups waited hours for a human to approve a plugin change.
  Asking early, with the exact change and the alternative, shortened the wait; a model-side workaround that patches
  the plugin at run time was rejected each time.
- **Response logging.** vLLM's native response-logging middleware drained the whole streaming body before
  returning, so streaming looked like one burst (time to first token = whole generation). A logging middleware
  that leaves streaming responses alone avoided it.
- **Reasoning and tool models** need the reasoning parser and tool-call parser flags and auto tool choice on the
  server; without them reasoning text leaked into answers and tool calls returned HTTP 400. A real tool call
  before the eval found it.
- **Template fields.** A pinned HF chat template expected `reasoning_content` while the current vLLM API emits
  `reasoning`; token counts differed until the field was aliased.
- **Token ids.** Without `return_token_ids` in eval requests, replaying a failure means re-tokenizing text, which
  is approximate.

## Eval infrastructure

- Agent eval containers filled the root disk through terminal-session logs copied into each container; a disk
  watchdog that truncates those files saved the second run.
- Docker's default address pool allows about 31 bridge networks; with one network per trial, fewer trials ran in
  parallel than configured. Changing the pool needs a daemon restart.
- Harnesses drift: a newer SWE-bench package broke the run (an older pin worked), an upstream Terminal-Bench task
  disappeared, and one harness ran a different benchmark than requested. Checking the first task's identity
  before a long run caught it.
- Output caps decide scores for reasoning models: answers cut at the cap count as wrong. Report them separately
  and rerun at a higher cap when the trajectory was still progressing.
- A streaming relay with a short idle timeout dropped long prefills; long prompts need keepalive or no idle cut.
- Air-gapped hosts break stock tooling that downloads interpreters or wheels; prepare them offline first.
- **Replies survive only in the server's response log.** lm_eval keeps only the final answer and mini-swe-agent
  drops capped replies. A logging flag value the server did not parse lost every reply of a run; one short request
  read back from the log before the first eval run catches it.
- **Watching long runs costs context.** Each look at a running eval in a short turn resends the whole context, and
  supervisors that agents built for themselves crashed mid-run. A detached run with a recorded exit file, waited on
  in long intervals, avoided both.
- **Server restarts are slow.** Restarting a large model's server on a new precision config was estimated at about
  15 to 30 minutes (model sizes unrecorded); time the first one before budgeting reruns.
- **Stop strings in reasoning.** A benchmark's textual stop such as `Question:` can occur inside native reasoning
  and cut it off; lm-eval's `until: []` with the token cap and EOS kept the protocol otherwise intact.

## Slow benchmark and eval runs

When a small probe says a run will be long, or device time does not explain wall time:

- If device time explains it, the fix is in prefill, decode, sampling or multi-device optimization.
- If not, measure queue wait, request scheduling, trace compilation and selection, synchronization, HTTP and
  client overhead, serialization, retries, timeouts, and eval-framework or task-environment setup. Fix the largest
  and repeat the probe before spending the budget on the full workload.
- Keep first-use and warmed measurements apart; shortening valid inputs or outputs hides the problem instead of
  finding it.
