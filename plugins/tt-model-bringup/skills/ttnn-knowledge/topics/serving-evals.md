# Serving and evals

## Plugin and vLLM

- **Top-k cap.** The shared device sampler handles top-k up to 32: the model's limit, the plugin's parameter
  formatting and the C++ op each cap it. A model whose recipe needs top-k 64 hits all three; raising only some of
  them reads other users' probabilities.
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
