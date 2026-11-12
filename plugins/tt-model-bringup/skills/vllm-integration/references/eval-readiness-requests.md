# Eval-readiness request probes (P2, P3)

Stage 9 runs two probes against the running vLLM server. They send what the eval harnesses send,
so a refused request, a slow sampling route or a misparsed answer shows up here and not in a CI
eval run hours later. Both write `doc/eval_readiness/<probe>.json` through the shared result
writer; the stage gate `scripts/check_eval_readiness.py --stage vllm` reads them.

## Get it right the first time

- Before the first serve, compare the request profiles in
  `runtime/readiness_check/eval_request_profiles.json` with the adapter's `model_capabilities`:
  every profile's `top_k` (and the checkpoint's `generation_config.json` `top_k`) should be
  at most `max_device_top_k`, and a seed must not force host sampling.
- Serve with vLLM's reasoning parser and tool parser for the model family
  (`--reasoning-parser`, `--enable-auto-tool-choice --tool-call-parser`). Read the model card and
  vLLM's parser list; do not write a parser when a built-in one fits.
- Keep the server log (`> server.log 2>&1`) and pass it to both probes. They read the parser and
  sampling settings from it.

## How to run

Start the server as the stage does (32 slots, the final sampling config), then:

```bash
cd <tt-metal checkout>
export PYTHONPATH="$TT_MODEL_BRINGUP_ROOT/runtime:$PYTHONPATH"
M=models/autoports/<model>
python -m readiness_check.run_harness_requests --server-url http://localhost:8000 \
    --model-dir $M --hf-model <hf-model-id> --server-log <server.log>
python -m readiness_check.run_chat_conformance --server-url http://localhost:8000 \
    --model-dir $M --hf-model <hf-model-id> --server-log <server.log> [--trust-remote-code]
python "$TT_MODEL_BRINGUP_ROOT/scripts/check_eval_readiness.py" --model-dir $M --stage vllm
```

Useful options:
- P2: `--users 1,32` and `--load-users 32` (the user count whose ratio is critical);
  `--profiles lm_eval_reasoning,agentic`; `--output-tokens 128`; `--ratio-max 1.15`.
  `--max-device-top-k` and `--seeded-sampling-policy` override what the probe reads from
  `tt/generator_vllm.py` and the server log.
- P3: `--cases c02,c06`; `--thinking on|off`; `--thinking-on-kwargs` / `--thinking-off-kwargs`
  (JSON `chat_template_kwargs`, when the template's switch is not `enable_thinking`);
  `--sampling '{"temperature": 0.6}'` (default greedy); `--max-tokens` (thinking budget).

Each probe prints at most about 40 lines. Raw data is under `doc/eval_readiness/<probe>/`.
P2 takes about 5 minutes with 4 profiles at 1 and 32 users; P3 about 5 minutes.

## P2 harness requests (`harness_requests`)

Profiles (`eval_request_profiles.json`, each with its source):
- `lm_eval_reasoning`: lm-eval `r1_gpqa_diamond` as TTI runs it: temperature 1.0, top_p 0.95,
  top_k 40 (task YAML), seed 42 (TTI propagates `EvalTask.seed`), max_tokens 32768;
- `lm_eval_non_reasoning`: lm-eval completions with temperature 0, seed 42;
- `model_card`: the checkpoint's `generation_config.json`, read at run time (skipped when it sets
  no sampling defaults);
- `agentic`: Harbor terminus-2 settings: temperature 1.0, top_p 0.95, no seed, no top_k;
- `plain`: greedy, no seed; the baseline for the ratio.

Other probes (P4, P5) read a profile by name with
`readiness_check.eval_request_profiles.get_profile(name, hf_model=...)`.

For each profile the probe sends one request with the exact fields, then runs `plain` and the
profile back to back with the same number of concurrent streamed requests on the same server.
ratio = profile TPOT / plain TPOT (median of the per-request streamed inter-token times). It also
predicts the plugin's sampling route and counts host-sampling lines in the server log.

| Check | Severity | Usual meaning | Fix |
|---|---|---|---|
| `accepted_<profile>` | critical; not waivable for `lm_eval_reasoning` | the server refuses a field the harness sends, e.g. top_k above the device cap with host fallback disabled (Kolibri: card top_k 128 > cap 32) | admit the request on the plugin's host route or widen the device top_k (below); never drop the field silently |
| `ratio_<profile>_32u` | critical, waivable | the profile samples on the host for the whole step | see "ratio" |
| `ratio_<profile>_1u` | advisory | same, at 1 user | same |
| `max_tokens_fit_<profile>` | advisory | harness `max_tokens` + prompt exceeds `max_model_len` | record in the release spec that the harness clamps it (TTI clamps `max_gen_toks`) |

### ratio

The TT plugin samples a whole step on the host when ANY row in it has
(`check_perform_device_sampling` in the plugin's `model_runner.py`):
- `temperature > 0` and `top_k` unset or above the declared `max_device_top_k`;
- a seed while `seeded_sampling_policy` is `host` (plugin config; default `auto`);
- penalties while the model declares `supports_device_penalties: False`;
- always-host fields: `min_p`, `min_tokens`, `logit_bias`, `allowed_token_ids`, `bad_words`,
  custom logits processors, top-N logprobs the device path does not serve.

The result's `predicted_route` and `route_reasons` name the trigger. Fixes, in order:
1. Declare a wider device `top_k` when the device sampler really supports it, with a test that
   the wider draw stays inside the candidates. Evidence: MoVA moved from 32 to 64; GPQA (top_k 40,
   seed 42) went from 243 to 62 ms per 32-user step, and the GPQA run from 6 h 46 min to 2 h 58 min.
2. Keep the plugin default `seeded_sampling_policy: auto`. Do not set `host` to make seeded tests
   reproducible.
3. Otherwise record the profile constraint for the release spec (for example: the agentic harness
   must send `extra_body: {"top_k": 20}`, or the eval runs with a host-sampling cost). Do not
   change the plugin for this; plugin changes follow [plugin changes](plugin-changes.md).

`top_k` unset with temperature > 0 also routes to the host. vLLM fills omitted fields from
`generation_config.json`, so a checkpoint that sets `top_k` covers the agentic profile; one that
does not leaves it on the host.

## P3 chat conformance (`chat_conformance`)

Twelve requests through `/v1/chat/completions` (greedy by default):

| Case | What | Severity |
|---|---|---|
| c01 | system message with list-shaped content parts | critical, not waivable |
| c02 | thinking on: answer in `content`, thinking in `reasoning_content`/`reasoning`, no tags in `content` | critical, not waivable |
| c03 | thinking off (skipped when the template has no switch) | advisory |
| c04 | tool call: JSON-looking string argument and nested object; arguments parse and match schema types | critical |
| c05 | tool-result turn, then an answer | critical |
| c06 | history whose assistant turns have NO reasoning field (Harbor terminus-2) | critical, not waivable |
| c07 | history with `reasoning_content` on assistant turns | critical |
| c08 | `continue_final_message` / `add_generation_prompt: false` | critical |
| c09 | about 4K-token system prompt with a fact to recall | critical (recall is advisory) |
| c10 | n=1 with stop strings; no stop string in `content` | critical |
| c11 | streamed c02 | critical |
| c12 | `response_format: json_object` | advisory |
| `render_c01/c06/c07` | server `/tokenize` prompt vs HF `apply_chat_template` | advisory |

A refused request (HTTP 4xx/5xx) fails its case. An empty required field, thinking tags in
`content` or a wrong tool-argument type fails it too. A `<case>_quality` row (advisory) notes a
wrong answer or a missing finish reason.

| Failure | Usual meaning | Fix |
|---|---|---|
| c02/c11 empty reasoning, or tags in content | no or wrong reasoning parser | pick vLLM's built-in `--reasoning-parser` for the family; a model-folder parser via `--reasoning-parser-plugin` only when none fits |
| c02 empty content, finish `length` | thinking ran out of `--max-tokens` | raise `--max-tokens`; if the model never ends thinking, it is a model or template issue |
| c04 no tool_calls | auto tool choice off or wrong parser | `--enable-auto-tool-choice --tool-call-parser <family>` |
| c04 `content is dict` | the parser decodes JSON-looking strings regardless of schema | a schema-aware parser in the model folder via `--tool-parser-plugin` (Muse did this) |
| c01 empty or wrong answer, `render_c01` differs | content-format detection renders list parts as nothing | `--chat-template-content-format string` (K2-7B: fixed all-zero chat evals) |
| c06 refused ("missing a thinking field") | the template requires reasoning on every assistant turn | record in the release spec that the harness sends reasoning back (MoVA: terminus-2 `interleaved_thinking: true`). The check stays critical: agents also drop reasoning on summarize/truncate paths (MoVA kept 2 of 5 Terminal-Bench trials failing that way). Serve a model-folder copy of the template that treats a missing field as empty (`--chat-template`), with the diff in the work log, or stop and ask the owner |
| `render_*` differs | server uses another template or kwargs | serve the checkpoint's template; check `--chat-template` and `--default-chat-template-kwargs` |

If HF `apply_chat_template` itself raises on the same messages, the original model shares the
behaviour: record it with `original_model: "same"`.

## Time box and limitations

Spend at most 2 h fixing one probe. Then write the problem into
`doc/eval_readiness/limitations.json` with `probe`, `check`, `reason`, `evidence` (an existing
path, such as the probe's raw file), `original_model` and `fix_hours`; the gate turns a waivable
critical into advisory. `accepted_lm_eval_reasoning`, c01, c02 and c06 are not waivable: they
make evals impossible. A fix that is a harness setting goes into the release
notes for the TTI spec; do not patch the server or the plugin around it.
