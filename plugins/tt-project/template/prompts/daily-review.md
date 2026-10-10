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
5. Self-efficiency: if this spec ends with `Self-efficiency audit` lines (`ttp audit <name>` prints
   them, `--json` the rows), grade them. Grade every ask, one line each, `human-only` or
   `avoidable`: was it truly blocked on access, funds, spend, review, merge, an irreversible step,
   a restriction or another human, or could the project have decided, fixed or retried it itself (a
   dead worker, a stuck hold, a failed run, a reversible choice)? A needless ask is a defect: for
   each pattern, a `harness:` follow-up with a concrete fix (an ask-gate rule, a prompt rule or a
   model-free recovery). Asks the gate refused are the gate working; a reason refused again and
   again needs a prompt rule. Asks the gate flagged were still sent: grade each like any other
   ask. Each cause of wasted runs (failed, retried, lost or continued runs)
   that will recur gets a follow-up too: fix it the same day, not in a later review. Then name the
   top 1-3 inefficiencies (wasted runs, turns that decided nothing, idle wakes, $ per outcome,
   review loops, long blocks or waits) with their cost and a fix. An override held past its premise (a pause, a mute, a temporary instruction possibly over)
   is retired if it clearly is over, else gets an end condition. A resource pause the user set is
   never lifted without the user's word: propose an end condition, or ask the user. In the
   summary: one line, `needless asks: N of M` and the top inefficiency. `nothing to grade`: say so
   in a few words.
6. Memory: retire entries that are stale, done or replaced: `ttp memory <name> --forget <entry>`
   (the name in [brackets]; it moves to memory/archive/). Keep restrictions, preferences and
   resources that still hold. Name what you retired in the summary. A (standing) entry is a duty
   that recurs: one finished instance does not make it done, and size is no reason to retire it.
   Retire one only with `--why "<its end condition, or the user's words ending it>"`.
7. Deferrals held in memory ("deferred", "once X", "N days after Y", "queue it when ..."): turn
   each into a follow-up with `start_after` (a delay such as `3d` or an ISO time in the home zone) and/or
   `start_when` (a read-only shell probe from the project root: exit 0 = start, 1 = not yet,
   under a minute; `landed:#<id>` for "once task <id> landed"), and a spec that names the entry
   it replaces. The coordinator adds it as a
   deferred task and retires the entry in the same turn, so the deferral is never lost.
8. Charter: read `tt-project/harness/CHARTER.md`. List each restriction that newer charter text on
   the same subject contradicts, quoting both exactly, in the summary as
   `stale restriction: "<old text>" (contradicted by "<newer text>")`, and each section whose own
   end (`Expires:`, `Until:`) has clearly passed as `stale restriction: "<heading>" (over: <what
   ended it>)`. Do not edit the charter: the coordinator retires them with `charter_update`
   `quote` or `replaces`.

`result.json` → `followups`: at most 5 concrete tasks, each worth its cost, plus the same-day fixes
from step 5 and the deferrals from step 7 (`{"title", "spec", "start_after", "start_when"}`). Use titles starting
`project:` or `harness:`. Recommend disabling or slowing any recurring job that wastes money.
`summary`: five lines max, for the user.
