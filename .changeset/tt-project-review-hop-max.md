---
"tt-project": patch
---

`tt-project`:

- review fixes: a daemon `Fix review #N` task resumes the session of the task it fixes (or the earlier fix), at most `review.hop_max` (default 2; 0 off) resumed hops deep in one lineage. Past the cap the fix starts fresh from the coordinator's context (its session fork, else the disk block) and the coordinator gets one `review_hop_cap` event per fix task. A fresh start ends the count. On a provider that cannot resume the code task's session (Cursor, or another session format) the fix starts from the coordinator's context, as every follow-up does; tests cover resume, the cap and the fallback.
