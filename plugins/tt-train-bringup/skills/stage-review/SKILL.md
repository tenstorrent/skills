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
- multi-device work appears in a single-chip bring-up.

Plan:

- the kernel inventory skips ops of the reference forward or backward, or claims
  a kernel exists without a path;
- the freeze map does not cover every parameter and module input under the
  recipe;
- the roofline target has no derivation, mixes forward-only FLOPs with a training
  step, omits dX or dW, or ignores recompute the memory budget forces;
- the memory budget omits optimiser state, saved activations at the target
  sequence length, or workspace, or draws no recompute conclusion.

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
  attribution to model code.

Optimisation loop:

- a change landed without a PCC harness rerun on every gradient, an MFU rerun
  with the baseline command, and a Tracy diff showing the targeted ops gone and
  nothing new;
- a change is justified by fewer ops or a cleaner graph rather than measured
  time against the best correct measured path;
- an item in the ranked table has no class, or its cost omits multiplicity
  (recompute, backward mirror) or counts ops instead of chains;
- a lower-cost math kernel was chosen over a higher-ranked overhead item that
  can go to zero, without a stated reason;
- a rejection is unearned: a first API or shape error, a single failed attempt
  at one rung of the ladder, or "not supported" without a minimal repro, an
  adapted retry, or `$autofix` evidence;
- a layout, glue or wasted item above the stated threshold remains with no
  cost, attempted fix and reason recorded;
- a useful-math kernel is called "near roofline" without the roofline figure
  and the measured fraction;
- the host/dispatch gap is not measured, or it grew without explanation;
- the exit claim does not match the plan's target and margin, or the final MFU
  does not reproduce the best iteration's number;
- the final path fails the PCC harness or loss match.

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

## Anomaly Ledger
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

## Main-Agent Follow-Up

1. Verify the cited artefacts exist.
2. On `more-work-needed`, do not mark the phase complete or `blocked`; treat the
   findings as the next work item, fix or `$autofix`, then rerun `$stage-review`.
3. On `clean-pass`, record the review output path in the work log.
4. Create local checkpoint commits for phase-owned changes in the tt-metal
   checkout. Never push from autonomous bring-up.
5. Record branch and commit SHA in the work log. Do not include unrelated dirty
   files; if phase-owned changes cannot be isolated, that is more required work.
