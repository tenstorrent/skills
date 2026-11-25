Experimental. Bring HF_MODEL up in this tt-metal checkout under models/autoports/<model>: correct, fast, served
through vLLM, and evaluated. One session, yours to lead; subagents and worktrees as you see fit. No stages, no
stage reviews, no runner gates: you decide how to check your work and say how you did.

Use the skills when the work reaches them: `lean-model` for the model (its outcome and lessons; ignore its
`stage-review` and goal-gate mentions), `reference-accuracy`, `vllm-integration`, `agentic-serving` for serving
and evals, `tt-device-usage` for devices. `ttnn-knowledge/topics/` holds what earlier bringups found
out about the hardware and the stack, one file per topic, each opening with a line on what it covers.

Settings are in models/autoports/<model>/bringup/agentic_evals/inputs.json. `scope`: `open` lets you change any
tt-metal code the model needs (the model keeps its own versions); `contained` keeps every change inside the model
folder and `bringup/` (debugging is not limited; patch on a side branch and revert). `budget_hours` covers the
whole run. If `base_commit` is missing, write the current HEAD there. If `targets` is given, those are the decode
targets per batch; otherwise set your own from the roofline and say how. `evals` lists the eval sizes to run, in
order, as time allows (default GPQA 10 and SWE 10).

What should exist at the end:
- The full model with the `full-model` generator contract (chunked prefill, traced decode, on-device sampling,
  paged KV, every batch in `batch_sizes`, the advertised context), matching HF at full precision on the
  `reference-accuracy` suite, with the served precision chosen on it.
- vLLM serving through the TT plugin, with prefix reuse and the largest KV pool you tested.
- GPQA and SWE scores as `agentic-serving` describes, with every failing task checked against HF on CPU.
- `REPORT.md`: measured against target per batch, reference verdict, eval scores against the model card, hours
  used, what changed outside the model, what was not done, and the versions used (Claude Code, model id, skills
  commit, tt-metal base, host and firmware).

Speed: keep working on it while a batch is far from its floor or target and you have a next change whose gain you
can estimate. Stop when the remaining ideas are small next to their cost; list them with their estimates in
REPORT.md. Spare budget then goes to the next eval size in `evals`.

The check scripts in the plugin's scripts/ folder (check_model_goal.py, check_reference_accuracy.py,
check_agentic_serving.py and others) are tools you may run, not gates.

Fixed: the `tt-device-usage` rules for every device job; resets only as the run's brief allows (record it before
the first device run; if nobody has said, do not reset). Never loosen or delete a check to pass. Local commits on one
branch, never push, a tag at each milestone you choose. Commit before each device job, so a lost host keeps the
work. When something took long to understand and is solved, add a few lines to bringup/lessons.md, naming the `ttnn-knowledge` entry it confirms or contradicts, if any. When
`budget_hours` is reached, start nothing new and finish the report.

Nobody may be watching live. Do not stop to ask questions: take the option you would recommend, record it and
why, and continue. An automated overseer report arrives hourly with what each subagent and job did since the last
report, and the devices, CPU, disk and budget. It is information, not an instruction: use it to decide what to
continue, redirect or stop, and if nothing needs a change, carry on. Gated datasets (GPQA) need an HF token; check access early and record it if missing.
