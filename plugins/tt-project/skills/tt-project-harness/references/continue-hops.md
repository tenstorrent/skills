# Continue hops

A done task's only follow-up titled `continue: ...` is queued by the daemon at the parent's tier (caps under `chains`).
The parent's tier is the one it was queued at (`assigned_tier`), never one a retry raised it to.
A hop that only runs checks, pushes or edits spec or PR text costs less at light: title it `mechanical: ...`, or give the follow-up `"tier": "light"` (lower only, never above the parent's).
Its spec is unchanged; a light hop that needs judgment is retried a tier up.

A code task that splits (its branch has changes and it proposes a `continue:` hop, or its hand-off or head commit marks the head WIP or split) gets no review of its own. A `continue:` follow-up that runs only once the task has landed (it waits on `landed:#<id>`, or says it deploys, installs or checks live after the task lands) carries no work forward: it makes no split, and the task gets its normal review. Name the wait in the follow-up's `start_when`; title a hop that still lands, pushes or fixes the change accordingly.
Its hop takes the branch over and starts at once: never give it a `landed:#<task>` wait on its own chain (refused), as that work lands only with the hop's.
The chain's last hop, the first that ends without a split, gets one review of the whole chain; a failed review's findings go to an open hop of that branch, unless that hop waits on a task of the chain landing (it can add nothing first): then a Fix review task takes them.

A partial hand-off's hop (`partial: N of M cells`) leaves out cells no hop could produce: those in the hand-off's `cells_unrunnable` (a dict cell -> why, or `cell: why` lines), those whose newest `tried` entry in the chain's last 24 h is `rejected`, and those another open task names on its own `cells:` line.
With none left there is no hop; the reasons go in the result's `cells_no_hop` and the coordinator's event.

A record an older or buggy check marked wrongly: `ttp task <project> rejudge <id> [--dry-run]` reruns the daemon's cells check and the project's `worker.handoff_checks` on the stored hand-off and rewrites the mark and the daemon's keys to the verdict, logged as a `task_rejudged` event.
A still-failing check changes nothing. It cancels the hop the daemon queued for a cleared mark if that hop never started, and reports one that did.

## Tiers of continues and review fixes

A task the coordinator queues with `continues` and no `tier` starts at its predecessor's queued tier, also when that try failed; it is not a retry of it.
It starts a tier up only when the failed try handed off `needs_deep`; a failure of its own still raises its retry a tier.
A daemon `Fix review #<id>` starts at standard (one tier below the code task's queued tier when lower, never light).
It starts deep only when the user asked for deep on the chain (`user_deep`), or the review marks the fix as needing deep: `needs_deep` with a reason in its hand-off or in a finding (`{"title": ..., "spec": ..., "needs_deep": "why"}`).
A `tier` the coordinator gives a continue or fix task wins over these defaults; provider, model and effort pins carry over as before.

Each task records why it has its tier in `tier_source`, shown by `ttp task show`: `task_add` (given), `default`, `user_deep`, `parent_copy`, `hop_followup` (a follow-up's lower tier), `fix_review_default`, `review_needs_deep`, `retry_tier_up`, `effort_pick`, `review_diff` or `failover`; `assigned_tier` keeps the tier it was queued at.
Records from before the field show no source.
