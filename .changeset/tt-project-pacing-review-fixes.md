---
"tt-project": patch
---

`tt-project`: tighter spend and lock guards for plan pacing and parallel workers.

- A plan provider whose last window reading is over 30 minutes old, with two paid runs ended since,
  is treated as usage-billed: its own gate applies the daily and weekly caps until it reports
  windows again.
- Under the dollar caps, a dispatch tick starts a run only if its budget fits in what is left of
  the caps after what running work may still spend. Tasks that do not fit stay queued; a task
  whose budget is above a cap is blocked with that reason.
- An `exclusive:<resource>` task holds a slot of that resource's lock for its whole run, so
  other tasks' `ttp lock` commands wait for it (its own pass straight through), and it does not
  start while a `ttp lock` command holds the resource.
- `ttp lock` reports a wait in the run's progress, so a wait is not killed as a stall. Inside a
  run it gives up after half the run's stall limit by default and exits 75; workers then hand the
  task back as `waiting`.
- The idle-slot coordinator wake fires only on a plan that is green and burning slower than its
  pace needs, with nothing queued, no open question and the daily task cap not reached. A wake
  that adds no task doubles the wait for the next one, up to the idle wake.
