---
"tt-project": patch
---

`tt-project`: spend projections stop at the cap's reset. `ttp status`, the web header and the coordinator's STATE budget line now say when each daily dollar cap is reached at the current burn rate (the last hour's readings, else the period's average): `at $86/h: not reached before the reset at 08:00 PST`, or `at $400/h: reaches the cap at 07:48 PST, before the reset at 08:00 PST`, in the project's home zone. A reach time after the reset is never shown; at or over the cap the line names the reset; an unknown reset (rolling 24 h) or an unmeasured rate says so and projects nothing. The coordinator prompt tells it to quote this projection and never compute one that ignores the reset.
