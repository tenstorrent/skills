---
"tt-project": patch
---

A resumed Codex run books its own tokens, not the whole session's. `codex exec resume` reports the
session's running totals on turn.completed, so a resumed run of about $0.75 was booked at $31.66. The
daemon now subtracts a baseline it can prove: the session's rollout total before the run started, else
the counters the session's previous run recorded (note `thread_totals`). With neither, or with counters
that started over, the run keeps the reported figure, marked `own_basis: unverified`. The runner's
budget check (`cost_so_far`) uses the same figure. A one-time repair re-books past resumed Codex runs
that have a provable baseline, keeps the old figure in the run's note, moves the task's spend and the
ledger by the difference, and writes a receipt under `state/receipts/`. Claude and Cursor booking is
unchanged.
