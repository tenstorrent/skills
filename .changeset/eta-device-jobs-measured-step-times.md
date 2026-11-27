---
"tt-project": minor
---

ETA from measured device step times: a task that runs detached jobs gets its expected
finish from the medians of same-named jobs in this project's history (names normalised
by dropping a trailing run word or id), plus the measured start-up wait and its queue
position behind another open task's running job of the same name; the conservative time
is each job's p80. With no sample for a job name the estimate says so instead of a padded
default. The coordinator's digest shows the measured estimate beside a promise on such a
task, and the eta_history shadow baseline uses the same measured job medians.
