---
"tt-project": patch
---

`tt-project`: a failed review whose spec also names another change's branch (a coordinator note) gets its fix task and re-review on the branch it depends on, instead of none, and never a second fix when a `Fix review #<id>` task already exists; a failed review whose fixes the daemon cannot queue (a capped round, branches that are not one stack, a task cap) raises one `review_nofix` event naming the review, its branch and what waits on it, and one with no fix follow-ups says so, with its branch and what waits, in the `task_failed` event that wakes the coordinator; and a start_when that is exactly `landed:#<id>` whose producer is done, not landed, with no open review, fix, rebase or push row that can land it, raises one `landed_wait_dead` event naming the held tasks (after `waiting.landed_wait_dead_s`), cleared once a path appears, the work lands or nothing waits on it, judged from saved task rows only, so a daemon restart neither clears nor repeats it.
