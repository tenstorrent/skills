---
"tt-project": patch
---

`tt-project`: `ttp push` runs to one branch take turns, and waiting for a turn is cheap.

- Each push holds a lock for its target branch from its first fetch to the push, so two reviewers
  no longer race each other through rounds. A killed push or a reboot frees the lock.
- A push waits at most `delivery.push_wait_s` (default 300 s) for its turn; the wait does not count
  against the run's wall clock. Past that it exits 75 and prints who holds the turn and a
  `retry_when` for the hand-off: `ttp push --free`, which exits 0 once the turn is free.
- Reviewers run `ttp push` in the foreground with their longest tool timeout and hand off
  `waiting` with the printed `retry_when` on exit 75.
