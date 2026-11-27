---
"tt-project": patch
---

`tt-project`: checks admission on shared hosts. Niced CPU work (above the checks runner's own nice) that does not belong to a running check no longer counts as busy, so another project's batch work no longer holds every check; the harness's own checks still count, and unreadable process stats keep the old reading. The checks running now no longer shrink the host cap they hold, which stays at most cores // check price; a host that is really full still refuses a start. Schedule precheck evidence: only exit 1 binds its JSON, a `captured_at` that is neither an ISO time nor an epoch binds unknown, and an unknown binding reads as no precheck evidence in the run's spec. ETA comments name the detached-job helpers by their current names.
