---
name: stage-review
description: Independently review a tt-train training bring-up phase (plan, composite bring-up, optimisation loop) before it is allowed to pass. Use when a phase is complete or near-complete and a fresh subagent must compare the phase output against the goal contract, the phase skill, logs, PCC and loss evidence, MFU and Tracy artefacts and code; identify bugs, contradictions, weak dismissals or unearned rejections; and return a clean-pass or more-work-needed verdict.
---

# Phase Review

Before using this workflow, follow the dependency and environment setup in
[the train-bringup entrypoint](../train-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

## Mission

Prevent unsupported phase closure. A review is an independent, inspection-first
comparison of one phase against its goal contract and evidence. Passing tests
are a floor, not proof. The reviewer looks for anything that should be
investigated or fixed in the spirit of a correct, fast training model, even when
every scripted check passes.

## Required Inputs

Tell the reviewer exactly what it is reviewing:

- phase name (`train-plan`, `composite-bringup`, `optimization-loop`) and, for
  the loop, the iteration range;
- target model, recipe and model directory;
- the original goal text or a concise contract copied from it;
- the phase skill path;
- evidence roots: `doc/plan.md`, `doc/perf_analysis.md`, `doc/<phase>/README.md`,
  `work_log.md`, test logs, Tracy CSVs, MFU logs;
- branch/commit and whether the worktree is live or a snapshot;
- any user instruction that constrains the review, but not your theory of what
  is wrong.

## Invocation Modes

Main-agent mode: when you own the phase, spawn a fresh subagent for the review.
Prefer passing raw paths and a compact contract over passing your conclusions.

Reviewer mode: when your prompt says you are the independent reviewer, perform
the review directly and do not spawn another reviewer. Verdict is `clean-pass`
or `more-work-needed`.

If no subagent tool is available, do a serial review as a temporary fallback and
mark it `not-independent`. That is not a verdict and never counts as a pass.

The reviewer is read-only unless asked to write a report. It may run read-only
commands and small analysis scripts over artefacts. It must not open TT devices,
reset devices, launch training runs or profiles, or mutate implementation files.
If confirming a problem needs hardware, report that as a finding.

## Cadence And Severity

In the optimisation loop a review runs every eight iterations or at a milestone (a kernel landed,
a target crossed, a close claimed), not after every iteration or two: the reference bring-up spent
19 of 90 commits answering 15 rounds that found two real problems. A clean-pass never ends the
agent's turn while the target is unmet.

- P1: a correctness break (harness, loss, a silent script), a bisect break, a result row that
  cannot be reproduced, or a forbidden mode run. P1 is fixed and re-reviewed.
- P2: everything else, including document hygiene (a misquoted figure, a stale table, wording).
  All P2 items of one round are fixed in one commit and are not re-reviewed; the next scheduled
  review checks them.

## Review Stance

- Treat agent-written READMEs and work logs as claims; re-derive them from test
  logs, JSON, CSVs and code.
- Treat passing checks as insufficient when artefacts contradict them.
- Treat `minor`, `known`, `remaining`, `mostly`, `except`, `waived`, `xfailed`,
  `out of scope`, `should be`, `expected to` as review triggers.
- Prefer precise required-work findings over broad advice.

## More-Work-Needed Standard

Return `more-work-needed` when evidence shows one of these.

Any phase:

- a required artefact, metric, test or implementation path from the contract is
  missing, failing, stale or contradicted by another artefact;
- a hidden size, head count, sequence length, batch or weight shape was reduced
  for evidence without being labelled as debugging only;
- a material anomaly is dismissed with prose instead of investigation;
- multi-device work appears without a multi-chip fit decision in the plan and a recorded user
  authorisation; or the model does not fit one chip and the work is still single-chip.

Plan:

- the kernel inventory skips ops of the reference forward or backward, or claims
  a kernel exists without a path;
- the freeze map does not cover every parameter and module input under the
  recipe;
- the roofline target has no derivation, mixes forward-only FLOPs with a training
  step, omits dX or dW, or ignores recompute the memory budget forces;
- the memory budget omits optimiser state, saved activations at the target
  sequence length, or workspace, or draws no recompute conclusion;
- the plan has no hardware-ceiling section: no clock ladder with telemetry, no limits and firmware
  bundle, no critical chip, no multi-chip fit decision, or a host/dispatch gap is budgeted by
  assumption;
- the target is cycle time, a single-chip number for a model that needs the box, or MFU re-based to
  the delivered clock instead of the nominal peak with the delivered clock as a stated margin;
- the top three ops of the step lack their compiled kernel source, sync mode and % of peak, or the
  hard-constraints block is missing.

Composite bring-up:

- the PCC harness skips any gradient, any module or any layer kind, or compares
  against a bf16 torch reference instead of fp64 with bf16-rounded inputs;
- PCC is reported without relative error, or a tensor passes PCC with a visible
  bias in relative error;
- the loss comparison has no stated tolerance or no torch trajectory;
- a backward op allocates or writes a gradient for an input the freeze map marks
  frozen;
- the baseline MFU was taken with profiler environment variables set, or the
  MFU FLOP count differs from the plan's;
- `doc/perf_analysis.md` lacks the ranked cost table or the table lacks op
  attribution to model code;
- Tracy was run on the full layer stack, or on fewer than four doubling depths,
  or a depth is not a whole number of blocks (the smallest repeating group of
  layer kinds, so a heterogeneous model such as Qwen3-Next's 3+1 pattern is
  never cut mid-block), or a depth's residual from the fitted line exceeds the
  stated tolerance without an explanation, or the projection was not validated
  against the full-model MFU wall-clock;
- the plan's layout is multi-chip and the harness never ran the per-rank shard shapes, or the loss
  match ran on one chip instead of the layout, or the loss script can end without a log and still
  count as a pass;
- the baseline row has no telemetry (settled clock, critical chip) or the host/dispatch gap was
  computed from profiler cycles at the nominal clock.

Optimisation loop:

- a change landed without a PCC harness rerun on every gradient, an MFU rerun
  with the baseline command, and a Tracy diff showing the targeted ops gone and
  nothing new;
- a change is justified by fewer ops or a cleaner graph rather than measured
  time against the best correct measured path;
- an item in the ranked table has no class, or its cost omits multiplicity
  (recompute, backward mirror) or counts ops instead of chains;
- an overhead item was chosen over the largest math kernel without the chain-deletion reason
  (its measured cost above the kernel's expected gain), or iteration 1 was not a kernel attempt on
  the biggest math item, or the top ttnn kernel has no kernel attribution (source, sync mode, % of
  peak);
- a rejection is unearned: a first API or shape error, a single failed attempt
  at one rung of the ladder, or "not supported" without a minimal repro, an
  adapted retry, or `$autofix` evidence;
- a rejected item has no retry-when condition, or its condition came true and it was not retried;
- a layout, glue or wasted item above the stated threshold remains with no
  cost, attempted fix and reason recorded;
- a useful-math kernel is called "near roofline" without the roofline figure
  and the measured fraction;
- the host/dispatch gap is not measured, or it grew without explanation;
- the exit claim does not match the plan's target and margin, or the final MFU
  does not reproduce the best iteration's number;
- the final path fails the PCC harness or loss match;
- the loop is closed, or the turn ended after a review, with the target unmet and a remaining item
  whose "near roofline" or "inherent" claim has no roofline figure, measured fraction and kernel
  attempt (collectives: no overlap attempt);
- an iteration carries no kernel-level change and the Python-level change did not delete a whole
  op chain, with no reason stated;
- the overhead classes are below the step's noise and the iteration still removes glue instead of
  boosting one of the largest kernels; or a kernel rewrite starts without the roofline, byte and
  core accounting of the "Kernel boost checklist", or lands below half the expected gain without the
  reason and the next structural fix;
- a precision mode, run type or configuration the hard-constraints block forbids was evaluated,
  swept or recorded as a rejected candidate;
- a subset-depth MFU run, a projection or a Tracy table appears in the result table or the report
  as a result rather than as ranking evidence;
- `doc/perf_analysis.md` or the README deviates from the optimisation-loop document format and
  report style: a "what changed" cell that names a path or a file instead of the mechanism, a cell
  too terse to be understood without the commit, a missing `tag` or `time since start` column, a
  result without its ΔMFU, prose longer than three lines, sentences in table cells, narrative of
  attempts without a number, review caveats appended instead of cells corrected, or an in-house
  word ("ledger", "rung") used as a section or column name;
- three or more consecutive iterations carry no ΔMFU while the two largest rows of the cost table
  are closed on a pre-check that falsified one mechanism rather than on a data-flow attempt;
- a ceiling or "target unreachable" claim is supported by a sum of unbuilt ceilings rather than by a
  roofline figure and a failed kernel attempt per remaining row;
- a python-side measurement was taken after a build of a library target only, without the extension
  the run loads being rebuilt and its new path shown to execute;
- a result row reports MFU at the delivered clock, lacks the per-chip clock and critical chip, or
  rests on an A/B of standalone numbers from different processes;
- `git add -u` or a commit touched a submodule pointer, or a tracked evidence file is over 500 KB,
  or a Tracy dump sits inside the repository tree;
- a commit bundles more than one code change so the history does not bisect (one commit for a
  review round's P2 document fixes is fine).

Do not return `more-work-needed` only because a stronger evidence format would
be nice. If the skill accepts tests, logs and CSVs as evidence, do not invent a
new required artefact unless the existing ones are only prose, do not tie the
claim to the measured path, or contradict it. Put such requests under
`Hard-Check Gaps`.

`more-work-needed` is a remediation trigger, not permission to set the goal to
terminal `blocked`. The phase owner fixes each finding directly or with
`$autofix`. Only a later explicit `$autofix` failure or an unrecoverable
external dependency justifies `blocked`.

## Anomaly Rule

Every anomaly noticed by the phase agent or the reviewer must be classified
before pass:

```text
Observed anomaly:
Evidence:
Affected path:
Control or comparison:
Likely subsystem:
Investigation performed:
Resolution: fixed / controlled / more-work-needed
```

Prose acknowledgement is not resolution.

## Reviewer Prompt Template

```text
Use $stage-review as an independent reviewer for one tt-train training bring-up phase.
You are the fresh review subagent. Do not spawn another reviewer. Do not modify
implementation files. You may run read-only local commands and small
artifact-analysis scripts over existing artifacts. Do not open TT devices or
run training or profiling.

Phase under review: <train-plan | composite-bringup | optimization-loop, iterations a..b>
Target model and recipe: <hf model id>, <recipe>
Model directory: <absolute path>
Original goal contract:
<paste concise goal contract>

Phase skill to compare against:
- <absolute path to SKILL.md>

Key artifacts to inspect first:
- <doc/plan.md, doc/perf_analysis.md, doc/<phase>/README.md, work_log.md>
- <test logs, Tracy CSVs, MFU logs>
- <implementation paths>

Review task:
1. Read the phase skill and the evidence.
2. Compare the delivered artifacts against the goal contract.
3. Re-derive PCC, relative error, loss, MFU and ranked-table claims from raw artifacts.
4. Look for bugs, contradictions, stale artifacts, unearned rejections and weak dismissals.
5. Return the verdict format below. Lead with findings.
```

## Verdict Format

```markdown
# Phase Review

Verdict: clean-pass | more-work-needed

## Required Work
- P1/P2: <title>
  Evidence:
  Why this matters:
  Required next step:

## Other Concerns
- ...

## Hard-Check Gaps
- ...

## Anomalies And Their Controls
- Observed anomaly:
  Evidence:
  Affected path:
  Control or comparison:
  Likely subsystem:
  Investigation performed:
  Resolution:

## Scope Inspected
- Goal/skill paths:
- Artifact paths:
- Code paths:
- Commands run:

## Residual Risk
- ...
```

Only `clean-pass` with no required work satisfies this skill.

Write the verdict in the loop's report style: a finding is one line of what is wrong with the
number or cell that shows it, and one line of what to do. No restated method, no praise, no
hedging. A verdict under forty lines is the norm; findings the phase owner cannot act on in one
commit are not findings.

## Main-Agent Follow-Up

1. Verify the cited artefacts exist.
2. On `more-work-needed`, do not mark the phase complete or `blocked`; treat the
   findings as the next work item, fix or `$autofix`, then rerun `$stage-review`.
3. On `clean-pass`, record the review output path in the work log.
4. Create local checkpoint commits for phase-owned changes in the tt-metal
   checkout. Never push from autonomous bring-up.
5. Record branch and commit SHA in the work log. Do not include unrelated dirty
   files; if phase-owned changes cannot be isolated, that is more required work.
