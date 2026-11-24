---
"tt-project": minor
---

Held machines: a model-free inventory of every project's live work on a machine (shared lock holders, open tasks naming it, standing machine-change setups, schedules and watcher scripts naming it), shown as 'still on <alias>: ...' in `ttp status` and `ttp machines status`, with one note to each owner project when a hold or known outage is placed. The worker guard refuses ssh, scp and rsync to a machine under an active machine-wide hold or whole-machine known outage.
