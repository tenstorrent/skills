# Daily review

Assess the last day of this project, then hand off.

1. Progress against the charter's goals. What moved, what stalled, why.
2. Spend: `ttp status <name> --json` → budget. Top spenders, runs without durable progress,
   recurring work that is not earning its cost. If this spec ends with `Jev uses` lines, give each
   use's net saving and error rate in one line; the daemon already switches off a use that saves nothing.
   `ttp stats <name> --days 1`: context re-read (cache-read) tokens per run and per $, and the runs
   that re-read most. Coordinator prompt cache (`budget.coordinator_cache`: hit %, turns that missed,
   $ per turn, 24 h against 7 d): say if the hit rate fell.
3. Quality: failures, retries, flaky areas, slop or duplication introduced, architecture drift.
   Count reviews that asked for changes (`changes_needed`) apart from failures: those reviews worked.
4. Harness friction: where the user was needed, where the coordinator mis-planned, slow loops.
   Unblocking quality: if this spec ends with `Unblocking quality` lines, report them in one short
   section: what is stuck now, time in each stuck state (blocked, waiting, review: count, median,
   p90, longest) and whole stuck episodes, asks the user handed back ("decide yourself") with their
   ids and blocking reasons, and the coordinator's high/low turn split and escalations. Keep their
   labels: inventory is not a 24 h count, cumulative counters are not daily totals, and an unknown
   is not zero. A handed-back ask should not have been sent: say what rule
   would have let the coordinator decide it, as a `harness:` follow-up when one is missing.
5. Memory: retire entries that are stale, done or replaced: `ttp memory <name> --forget <entry>`
   (the name in [brackets]; it moves to memory/archive/). Keep restrictions, preferences and
   resources that still hold. Name what you retired in the summary. A (standing) entry is a duty
   that recurs: one finished instance does not make it done, and size is no reason to retire it.
   Retire one only with `--why "<its end condition, or the user's words ending it>"`.
6. Deferrals held in memory ("deferred", "once X", "N days after Y", "queue it when ..."): turn
   each into a follow-up with `start_after` (a delay such as `3d` or an ISO time) and/or
   `start_when` (a read-only shell probe from the project root: exit 0 = start, 1 = not yet,
   under a minute; `landed:#<id>` for "once task <id> landed"), and a spec that names the entry
   it replaces. The coordinator adds it as a
   deferred task and retires the entry in the same turn, so the deferral is never lost.
7. Charter: read `tt-project/harness/CHARTER.md`. List each restriction that newer charter text on
   the same subject contradicts, quoting both exactly, in the summary as
   `stale restriction: "<old text>" (contradicted by "<newer text>")`, and each section whose own
   end (`Expires:`, `Until:`) has clearly passed as `stale restriction: "<heading>" (over: <what
   ended it>)`. Do not edit the charter: the coordinator retires them with `charter_update`
   `quote` or `replaces`.

`result.json` → `followups`: at most 5 concrete tasks, each worth its cost, plus the deferrals
from step 6 (`{"title", "spec", "start_after", "start_when"}`). Use titles starting
`project:` or `harness:`. Recommend disabling or slowing any recurring job that wastes money.
`summary`: five lines max, for the user.
