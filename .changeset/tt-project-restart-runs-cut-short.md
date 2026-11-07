---
"tt-project": patch
---

Runs a reboot or crash cut short restart themselves: a worker run lost to a reboot resumes its session
however short it was; one whose process died without a result on this boot restarts once with no
attempt spent (a second loss in a row counts one); a review precheck a reboot killed starts again
once per boot while its review is open; relaunched checks keep their place in the host checks
queue. The Host line reads "N runs cut short: R resumed (K with work kept), L not restarted". A task deferred
since its hand-off (start_when, start_after) gets none of its killed runs restarted; a resumable
`ttp detach` job a probe marked lost to the reboot, or one a `survives_reboot` wait also checks,
is relaunched; each job relaunch not made says why in daemon.log.
