---
"tt-project": patch
---

`tt-project`: a resource hold kept for the detached jobs of a done, failed or cancelled task raises one
low alert (key `hold-stale:<run>`) once it has held the resource longer than `budget.stale_hold_alert_s`
(default 6 h). The alert names the task, the resource and the jobs' .rc paths, and clears itself once the
hold ends. The hold is never released or its job killed automatically.
