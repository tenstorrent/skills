---
"tt-project": patch
---

tt-project: the daemon runs `delivery.post_land_checks` after each landing, detached and without a model. A failure
that passes on a rerun is recorded as flaky (quarantined, not reverted); one that fails before the landing too is
not the landing's. A new failure is bisected over the landed changes to the guilty one, whose revert is approved
into the push queue (never forced), and one code task is queued to fix it on its failing tests alone. The bisect follows the changes' order on the
branch, and a revert takes only the guilty change's own commits; the fix task starts once that revert has landed
and names the reverted range. A run the test lock keeps busy backs off and retries, and the landing is checked
again on the next pass, never skipped.
