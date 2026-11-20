---
name: datatype-sweep
description: Sweep and select TTNN model weight, activation, KV-cache, CCL datatype, and compute-fidelity policies for the fastest configuration that satisfies minimum full-model top-1 and top-5 accuracy. Use after optimized-full-model and before vLLM, or whenever a runnable TTNN full model needs evidence-backed precision selection.
---

# Datatype Sweep

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

## Mission Context

Start from a working optimized TTNN full model and choose the fastest precision configuration that still meets a
stated full-model accuracy bar. It normally runs after optimized full model and before vLLM, so serving inherits a
settled weight, activation, CCL, KV-cache dtype and compute-fidelity policy. How dtypes, fidelities and their
failure modes behave: [matmul and precision](../ttnn-knowledge/topics/matmul-precision.md) and
[numerics](../ttnn-knowledge/topics/numerics.md).

- **Complete policy.** The selected artifact lets later stages construct the policy mechanically: weight dtype
  groups, layer exceptions, compute fidelities, activation/residual dtype, CCL dtype, KV-cache dtype,
  logits/sampling dtype assumptions, and any loader or runtime flags. "BFP8 weights" alone is incomplete.
- **Consumed policy.** The runtime construction path consumes every selected dtype and fidelity field. Prove it
  with a model summary, a config propagation check or profiler/perf-report rows from the measured candidate. A field
  in `selected_precision_config.json` that the code ignores or hard-codes differently leaves the sweep incomplete.
- **Source of truth.** Full-model top-1/top-5 decides. Decoder-layer PCC and component timing only order candidates
  and debug surprises. Candidates also run the [`reference-accuracy`](../reference-accuracy/SKILL.md) quick tier and
  the selected config its full tier; the selection becomes that skill's new baseline.
- **Capability and context contract.** KV-cache dtype can change the largest feasible context: recompute
  `models/autoports/<model>/doc/context_contract.json` for the selected config. A candidate that passes only by
  lowering context or another advertised capability fails, unless a hard physical device limit prevents the
  advertised value and evidence shows the smaller value is the largest feasible for that candidate.
- **Non-aligned prompts.** A candidate that changes KV-cache dtype, cache layout, trace buffers or prefill chunking
  reruns the non-aligned prompt/prefill check before selection. An aligned-only faster candidate fails.

Default accuracy bar when the user gives none: top-1 >= 90%, top-5 >= 98%. Top-100 keeps the existing readiness
expectation unless the user or model-specific evidence changes it.

## Preferred Outputs

```text
models/autoports/<model>/doc/datatype_sweep/README.md
models/autoports/<model>/doc/datatype_sweep/work_log.md
models/autoports/<model>/doc/datatype_sweep/sweep_results.json
models/autoports/<model>/doc/datatype_sweep/sweep_results.csv
models/autoports/<model>/doc/datatype_sweep/selected_precision_config.json
models/autoports/<model>/doc/datatype_sweep/top1_perf_pareto.png
models/autoports/<model>/doc/datatype_sweep/top5_perf_pareto.png
```

The README leads with the selected config, top-1, top-5, top-100, TTFT, trace-verified teacher-forcing decode
t/s/u, post-selection token-out decode t/s/u when available, and the exact acceptance thresholds. Label each
performance number with its measurement regime and workload shape.

## Baseline

From the completed optimized full model, record:

- readiness reference provenance, preferably AIME24 with chat template and `--gen-len 100`; regenerate a reference
  with fewer than 100 generated tokens unless a concrete model or memory limit prevents it;
- `run_prefill_check` and `run_teacher_forcing` top-1/top-5/top-100;
- prefill TTFT and trace-verified teacher-forcing decode t/s/u (readiness runner, generator metrics or the model's
  benchmark harness);
- the active dtype policy for weights, norms, residual stream, CCL activations, KV cache, logits, sampling and MoE
  routing;
- a teacher-forced natural-text reference beside the readiness reference: real next tokens from long
  non-repeating text (the readiness book `models/tt_transformers/tests/tale-of-two-cities.txt.bz2`, several hundred
  positions), scored per position against HF. The readiness reference alone can select a policy that natural text
  rejects ([numerics](../ttnn-knowledge/topics/numerics.md#choosing-a-precision-policy));
- the baseline measured at least twice, serially, so the run-to-run spread is known before ranking.

Teacher-forcing decode performance counts only on traced decode. Eager or untraced results may help correctness
debugging but never rank candidates, enter the Pareto charts or select a config. If traced teacher forcing fails,
fix tracing (`$autofix` if needed) before continuing.

## Default Search

The coarse search finds most of the win in few full-model runs:

1. Evaluate the closest canonical performance and accuracy policies, if any.
2. BFP8 KV cache as a yes/no switch. A candidate that changes the KV-cache dtype also passes the short-prefix case
   (about 32 cached tokens) and the same-input per-user comparison at the largest batch in `batch_sizes`, at the
   full-model bar; a long-prompt layer PCC pass does not cover them
   ([matmul and precision](../ttnn-knowledge/topics/matmul-precision.md#precision-and-fidelity)).
3. BFP8 CCL or residual-transfer activations as a yes/no switch.
4. BFP4 for eligible MLP/expert matmul groups, excluding the first and last layer unless the canonical policy says
   otherwise.
5. If full-model accuracy fails, restore the highest-risk groups first until top-1 and top-5 pass. Default restore
   order, a heuristic to adapt to the architecture, profiler evidence and observed failures: FF2/down (and expert
   down); QKV/attention input; WO; FF1+FF3/gate+up (and expert gate+up). For MLA, shared experts, unusual norm
   placement, gated attention or fused projections, map groups to the nearest semantic operation and record the
   mapping. Back a failed BFP4 trial out to BFP8 first unless BFP8 is known to fail for that group.
6. Once an inner-layer config passes, optionally extend the surviving choices to the first and last layer; keep
   that only if full-model accuracy still passes.

Fidelity is swept with dtype ([Compute Fidelity](#compute-fidelity)). A dtype change can need a small code change
(the KV fill and update ops have different dtype contracts:
[prefill and KV](../ttnn-knowledge/topics/prefill-kv.md#paged-kv-basics)); run a one-decoder smoke test of each new
dtype before using or rejecting it in the full-model sweep.

## MoE Policy

Expert matmuls follow MLP policy: gate/up as FF1+FF3, down as FF2, shared experts as dense MLP unless evidence says
otherwise. Sweep routed experts on the active-expert path, not a dense all-expert debug path. Router logits, top-k
selection, routing scores, gate weighting and expert reduction weighting stay out of the coarse BFP4 sweep; BFP8 for
them is a low-priority trial. Record the route/top-k distribution or active expert count when it can vary. Why, and
how L1 geometry interacts with precision: [MoE](../ttnn-knowledge/topics/moe.md),
[memory](../ttnn-knowledge/topics/memory.md); a candidate that fails on L1 is not yet rejected on accuracy.

## Full-Model Evaluation

Every kept candidate is validated with full-model accuracy. Per config record:

- config id and precision config path;
- weight dtype groups and layer ranges;
- activation, residual, CCL, logits/sampling and KV-cache dtypes;
- top-1, top-5, top-100, token count and reference path;
- TTFT, trace-verified decode t/s/u, and evidence that the teacher-forcing decode path was traced;
- compute fidelity per material matmul group and evidence the measured runtime used it;
- direct comparisons for material same-dtype fidelity choices, especially BFP8+LoFi against BFP8+HiFi2 on
  decode-bound projections;
- whether the config passed the accuracy bar on every reference it ran on (the bar applies per reference; a
  candidate run on fewer references than the baseline is not comparable until it has);
- the binomial margin of each top-1 score; a one-token difference is noise, not a ranking; separate close
  candidates with the natural-text reference or more tokens
  ([numerics](../ttnn-knowledge/topics/numerics.md#choosing-a-precision-policy));
- exact command, branch/commit, hardware, mesh and environment notes.

Use `$qualitative-check` for prompt-based evidence: prompt-format metadata, rendered prompts or token ids, HF
controls, and classification of raw-completion or chat-template behaviour.

Rank candidates only on trace-verified teacher-forcing decode t/s/u.

Rerun `run_prefill_resume_check` (the resumed-prefill gate of
[prefill features](../vllm-integration/references/prefill-features.md)) on the selected policy. A precision change
can fail it with a correct cache; classify a failure with a per-position comparison against HF (the margin probe)
before treating it as a cache or position defect.

Teacher forcing is the selection metric, not the serving headline. After selection, run the warmed token-out
no-readback benchmark of optimized full model on the selected config through the normal construction path, and
record it separately as post-selection token-out performance. If it cannot run, say why; never present
teacher-forcing numbers as token-out numbers.

If layer-level decode, reduced token-out or an earlier optimized-full-model token-out artifact points to a different
fastest passing policy than teacher forcing, resolve it before selecting: run the same token-out benchmark for the
baseline and the top conflicting candidate through the normal construction path. A token-out path that fails
(generator or trace feedback) is a bug to fix or a recorded blocker, not a reason to use teacher-forcing throughput
as the serving answer.

## Anomalies

A degenerate reply (stray or repeated tokens, collapse into a few characters, a token loop) is a precision or stack
defect until shown otherwise, not a model answer. Goal 12 runs steps 1–5 on each anomaly, on each failed task whose
reference check says `implementation` (agentic-serving, Failure analysis; its CPU comparison replaces step 3's HF
run), and on a phase below its band, inside the phase's time. A phase below its band with no degenerate reply has
nothing to reproduce in step 2; its attempt is a precision check on its failed tasks: for each, the decisive call
(GPQA: the answer; SWE: the call whose action first went wrong), teacher-forced at served and at full precision
(top-1 agreement over the reply, probability of the chosen and of the correct continuation). Full precision that
agrees with served rules precision out: record it with `changed` saying so; one entry for the phase covers its
failed tasks. A disagreement goes on to step 4.

**Where precision comes from.** The served precision is the config this stage selected:
`selected_precision_config.json` (Preferred Outputs; the model's default construction path or the build kwargs
that load it). Its fields, this sweep's `sweep_results.json` and the model's precision plumbing (what
`build_generator` accepts) list the options. A change is a new config file passed by `--build-kwargs-file` to the
runners, or to the server with a restart; restarts of a large model are slow, so time the first and budget each one
([serving and evals](../ttnn-knowledge/topics/serving-evals.md#eval-infrastructure)). Record the file in the run's
`config.precision`.

1. **Detect.** On eval replies, agentic-serving's `scripts/reply_digest.py` reads every reply from the server's
   response log and flags a degenerate tail (zlib ratio or distinct characters of the last 4,096 characters); a
   flag is an anomaly. A failed or capped task it does not flag goes to a triage sub-agent, and the coordinator
   decides (agentic-serving, Failure analysis). `runtime/readiness_check/check_degenerate_output.py` catches doubled
   tokens and collapse in runner outputs.
2. **Reproduce.** From the failed trajectory take the model call that degenerated: its rendered input plus the reply
   up to the first stray token. Save it as a prompt file under `doc/agentic_evals/anomalies/` with the seed and
   sampling settings. Between waves, when no list run or rerun wave is using it, stop the eval server and run the
   prompt on the model runner, not the eval harness, at batch 1: `python -m readiness_check.run_autoregressive
   --prompt-file <file> --build-kwargs-file <config>` (free-running, with the HF continuation beside it), or a
   reference from `readiness_check.generate --prompt-source file` scored by `run_teacher_forcing`. Measure the
   anomaly rate over N short continuations, or the top-1 agreement and the probability of the correct next token at
   the failure point. It must run in minutes; shorten the prefix while it still fails.
3. **Full precision on TT:** `reference-accuracy`'s `full` configuration (BF16 weights, activations, KV cache and
   head, HiFi4). It only has to fit for this batch-1 test. If it still fails, run the prompt on the HF/GPU
   reference: if that passes, bisect the stack bug by op PCC; if it fails too, report model behaviour.
4. **Bisect to the fastest passing config** when full precision passes: by group (head, MLP gate/up, down,
   attention, activations), then by layer range, measuring decode t/s/u at each step. Choose the fastest config
   that passes; the next faster ones tried fail. A speed loss for accuracy is accepted.
5. **Re-measure and rerun.** Restart the server on the chosen config, re-measure only the stage-11 rows the eval
   uses at its batch (into `serving_rates.json`, labelled with the config), then rerun the eval list.

Goal 12 records each attempt in `results.json` `anomalies`: `phase`, `tasks`, `test` (command), `prompt` (file),
`seed`, `served` (`precision`, `result`), `full_precision` (`result`, `pass`), the HF/GPU result when step 3 needed
it, `changed` (what changed, or why nothing), `hours`; a precision fix adds `configs_tried` (`config`, `pass`,
measured `decode_tok_s_user`), `chosen` and `remeasured_rows`.

## Plots

Two matplotlib/pyplot Pareto charts, shown prominently in the README:

- `top1_perf_pareto.png`: x-axis top-1 accuracy, y-axis performance;
- `top5_perf_pareto.png`: x-axis top-5 accuracy, y-axis performance.

Performance is trace-verified teacher-forcing decode t/s/u; if only traced latency exists, convert it to
higher-is-better or label the axis. Never plot eager or untraced decode. Plot every evaluated full-model config,
draw the non-dominated frontier, mark the selected config in red, and draw a vertical dotted line at the minimum
allowed accuracy. Annotate enough points that the selected tradeoff and the rejected candidates read without the
raw JSON.

## Compute Fidelity

Fidelity is part of the selected policy, swept in the same candidate matrix as dtypes once the promising dtype
groups are known, guided by `tt-perf-report` from decoder or reduced full-model runs and by full-model accuracy.
Typical pairings (BFP4 with LoFi, BFP8 with HiFi2 or LoFi, HiFi4 or FP32 accumulation for sensitive BF16 ops):
[matmul and precision](../ttnn-knowledge/topics/matmul-precision.md#precision-and-fidelity).

- For every material matmul group tested with BFP4 weights, include a BFP4+LoFi candidate. BFP4+HiFi2 is a
  comparison or fallback, not a substitute. If BFP4+LoFi cannot run, record the exact TTNN/runtime blocker and keep
  the stage open until it is fixed or `$autofix` fails, unless BFP4 is already rejected for that group.
- Compare BFP4+LoFi against BFP4+HiFi2 where both are legal and the group is a meaningful decode cost: required for
  every selected BFP4 group, optional for rejected ones; the sweep table says which groups ran it.
- Sweep fidelity even when dtype is unchanged: for dominant decode projection groups include BFP8+LoFi and
  BFP8+HiFi2 when both are legal. Neither is assumed fastest or safe without full-model evidence.
- Record dtype and fidelity together in each candidate id.
- Record where a per-group fidelity field is not honored by the measured kernel (for example prefill matmuls whose
  fidelity is fixed by the weight dtype), so the table never claims a fidelity the op did not use.
- Keep a short table per material matmul group: weight dtype, fidelity, traced decode speed, accuracy, kept or
  rejected, reason.
- Prove the model code constructs the requested fidelity policy without hard-coded overrides.

Pass a candidate's precision config to the readiness runners with `--build-kwargs` or `--build-kwargs-file` (a JSON
object forwarded to `build_generator`), not environment variables or an edited default file. If the runtime does
not expose fidelity as a policy field, add that plumbing before ranking. Writing `compute_fidelities` into JSON the
measured model ignores does not satisfy this.

## Final Selection

Select the fastest config that meets the bar on every reference, also checking agent-shaped long prefixes
(`reference-accuracy`'s `agentic` and `long` slices) and the memory ledger
([`agentic-serving`](../agentic-serving/SKILL.md)), and free-running replies by the Anomalies guard. Higher
precision chosen for those reasons is a valid selection. Noise is the spread between the repeated serial baseline
runs: a candidate is a gain only when it beats the baseline by more than that spread; two configs inside it are a
tie, and the simpler, safer one wins.

Before finishing:

- make the selected config the model's default construction path, or a required config artifact that
  `build_generator`, full-model and vLLM paths consume by default;
- keep a simple config change or override that returns to the safe baseline;
- run the post-selection token-out no-readback check and record workload shape, trace status, TTFT, decode t/s/u
  and runtime counters;
- rerun the resumed-prefill check on the selected config at the default thresholds and keep its record beside the
  sweep evidence;
- run the `reference-accuracy` full tier on the selected config;
- rerun the shared qualitative prompt suite with `$qualitative-check` and HF controls; if visible text quality
  selects or rejects a candidate, the verdict comes from this prompt-correct suite;
- add a propagation check proving `build_generator` and the vLLM adapter load the same weight, activation, CCL, KV
  and fidelity policy as the winning sweep result;
- update the context contract for the selected KV-cache dtype and prove later construction paths use a matching
  max context.

If no lower-precision config passes, keep the baseline and leave evidence that the sweep tested the likely wins.
