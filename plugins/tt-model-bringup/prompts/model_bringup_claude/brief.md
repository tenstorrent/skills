Experimental. Bring HF_MODEL up in this tt-metal checkout under models/autoports/<model>: correct, fast, served
through vLLM, and evaluated. One session, yours to lead; subagents and worktrees as you see fit. No stages, no
stage reviews, no runner gates: you decide how to check your work and say how you did.

Use the skills when the work reaches them: `lean-model` for the model (its outcome and lessons; ignore its
`stage-review` and goal-gate mentions), `reference-accuracy`, `vllm-integration`, `agentic-serving` for serving
and evals, `tt-device-usage` for devices, `ttnn-knowledge` by topic.

Settings are in models/autoports/<model>/bringup/agentic_evals/inputs.json. `scope` is `open`: change any
tt-metal code the model needs; the model keeps its own versions. `budget_hours` covers the whole run. If
`base_commit` is missing, write the current HEAD there.

What should exist at the end:
- The full model with the `full-model` generator contract (chunked prefill, traced decode, on-device sampling,
  paged KV, every batch in `batch_sizes`, the advertised context), matching HF at full precision on the
  `reference-accuracy` suite, with the served precision chosen on it.
- vLLM serving through the TT plugin, with prefix reuse and the largest KV pool you tested.
- GPQA and SWE scores as `agentic-serving` describes, with every failing task checked against HF on CPU.
- `REPORT.md`: measured against target per batch, reference verdict, eval scores against the model card, hours
  used, what changed outside the model, what was not done.

The check scripts in the plugin's scripts/ folder (check_model_goal.py, check_reference_accuracy.py,
check_agentic_serving.py and others) are tools you may run, not gates.

Fixed: the `tt-device-usage` rules for every device job; resets only as the run's brief allows (record it before
the first device run; ask if nobody has said). Never loosen or delete a check to pass. Local commits on one
branch, never push, a tag at each milestone you choose. When something took long to understand and is solved,
add a few lines to bringup/lessons.md.
