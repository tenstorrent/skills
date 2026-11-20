---
"tt-project": patch
---

`tt-project`: fewer idle model wakes. The under-use alarm skips a detached job whose recorded state explains its idle time: it runs the project's checks (`ttp checks`), its task is deferred (a `start_after` still ahead or a `start_when` probe), it holds or is labelled with a paused resource, its spend cap stopped it, or the budget gate allows no new work. Such a job shows "idle by design: <why>" in its line and raises no `job_underuse` event or pulled-in check; an unexplained idle job still does. After a reboot, a detached job already over before the boot (its `.lost` marker or closed record predates it) no longer counts as cut short, so it neither wakes its task nor counts toward `budget.max_reboot_losses`.
