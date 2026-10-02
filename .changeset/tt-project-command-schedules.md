---
"tt-project": patch
---

`tt-project`: a coordinator `schedule_set` of kind `command` now stores its `command` (and
`timeout_s`) where the daemon runs it, and one without a command is rejected instead of reporting
"no command" forever. A schedule that fails twice in a row raises one alert that clears on its next
good run or when it is turned off; `ttp status` and the web app list failing schedules. Updating an
existing schedule keeps every field the action leaves out (interval, time, switch, budget,
description); only a new schedule gets the defaults. Turning a command schedule off never needs a
command, so a broken one can always be switched off.
