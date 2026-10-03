# Daily review

Assess the last day of this project, then hand off.

1. Progress against the charter's goals. What moved, what stalled, why.
2. Spend: `ttp status <name> --json` → budget. Top spenders, runs without durable progress,
   recurring work that is not earning its cost.
3. Quality: failures, retries, flaky areas, slop or duplication introduced, architecture drift.
4. Harness friction: where the user was needed, where the coordinator mis-planned, slow loops.
5. Memory: retire entries that are stale, done or replaced: `ttp memory <name> --forget <entry>`
   (the name in [brackets]; it moves to memory/archive/). Keep restrictions, preferences and
   resources that still hold. Name what you retired in the summary.
6. Deferrals held in memory ("deferred", "once X", "N days after Y", "queue it when ..."): turn
   each into a follow-up with `start_after` (a delay such as `3d` or an ISO time) and/or
   `start_when` (a read-only shell probe from the project root: exit 0 = start, 1 = not yet,
   under a minute), and a spec that names the entry it replaces. The coordinator adds it as a
   deferred task and retires the entry in the same turn, so the deferral is never lost.

`result.json` → `followups`: at most 5 concrete tasks, each worth its cost, plus the deferrals
from step 6 (`{"title", "spec", "start_after", "start_when"}`). Use titles starting
`project:` or `harness:`. Recommend disabling or slowing any recurring job that wastes money.
`summary`: five lines max, for the user.
