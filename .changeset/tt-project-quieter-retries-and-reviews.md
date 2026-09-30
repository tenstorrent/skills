---
"tt-project": patch
---

`tt-project`: fewer coordinator turns and worker runs when nothing needs a decision.

- A task the daemon requeues by itself no longer starts a coordinator turn: after a provider
  refusal or logout (which has its own alert), or after a run that crashed, stalled, was lost or
  ended without a hand-off. The final attempt's outcome still starts one, and so does a retry
  after a timeout, since that task may need splitting.
- The daily review skips a day in which no worker ran (other than the previous review) and the
  user sent no message. Existing projects get this too; a schedule can set `skip_if_idle` in its
  payload to opt in or out.
