---
"tt-project": patch
---

ttp checks: a request for the same checks on the same tree (repository, tree, commands, purpose and environment) joins the run already queued or running, whichever task asks (a worker's run, a detached rerun, the daemon's review precheck, a reviewer), and reports its verdict for its own head; a failed verdict goes only to the runs that joined it, so a later request runs again.
