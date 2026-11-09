---
"tt-project": patch
---

tt-project: the daemon runs `delivery.post_land_checks` after each landing, detached and without a model. A failure
that passes on a rerun is recorded as flaky (quarantined, not reverted); one that fails before the landing too is
not the landing's. A new failure is bisected over the landed changes to the guilty one, whose revert is approved
into the push queue (never forced), and one code task is queued to fix it on its failing tests alone.
