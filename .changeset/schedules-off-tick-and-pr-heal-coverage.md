---
"tt-project": patch
---

`tt-project`: every schedule, llm ones included, now runs off the daemon tick under its own timeout, so one slow schedule (a de-slop scan, a slow precheck) never holds the run_schedules step. The step names each schedule it starts with its kind and command, a step stopped at its budget with no named item names the runtime code line it was on, and a schedule that keeps timing out is one keyed observation (`schedule-timeout:<name>`). Health coverage now counts an enabled heal check that names an open PR, or its repo beside a PR word, as covering that PR (not report-only), and the coverage hint says how to name one.
