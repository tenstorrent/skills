# Running some stages, or picking up a partly brought-up model

Use when the user asks for one stage or a range, or when code for the model already exists:
from an earlier run of this workflow, from another workflow, or written by hand.

## 1. Inventory (part of stage 0)

Record in `run.json` under `inventory`:

- the model's code in the target directory: pipeline, components, tests;
- earlier stage summaries in the run root, if any, and their status;
- existing outputs: sample media, quality scores, perf reports, Tracy captures;
- for a model ported outside this workflow, the closest in-tree model it already follows.

Read the existing code and tests before changing anything. Do not rewrite working code, rename
files, or move tests to fit this workflow; add what the requested stages need next to it.

## 2. Adopt earlier stages

For each stage before the first requested one:

| Situation | Action |
|---|---|
| A `SUMMARY.md` from this workflow says PASSED | Trust it. Re-run its gate only if the code it covers has changed since its commit |
| No summary, but tests or outputs exist | Run the existing tests that cover the stage's gate on the target hardware. Write a summary with status `ADOPTED`, recording what passed, what failed, and what the gate needs that nothing covers |
| Nothing exists | Write a summary with status `NOT RUN` |

Adoption checks what is there; it does not fill gaps. Writing missing unit tests is stage 2
work, done only if stage 2 is in the requested range.

## 3. Prerequisites

Each stage in `stages.md` lists **hard** prerequisites (it cannot run without them) and **soft**
ones (it can run, but the result is less trustworthy).

- Hard prerequisite missing: do not run the stage. Write its summary with status `BLOCKED`,
  naming what is missing and the stage that produces it, then stop. Offer to run that stage.
- Soft prerequisite missing, failing, or `NOT RUN`: run the stage, and list the gap under
  "Needs review" in the stage's summary and in `BRINGUP.md`.

## 4. Run and stop

Run the requested stages in order with their normal gates and summaries. Stop at the end of the
range even if the next stage could start. Update `BRINGUP.md` and `bringup_report.json` to show
every stage's current status, including adopted and not-run ones.

A later request for more stages reuses the same run root and `run.json`.
