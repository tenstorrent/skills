---
"tt-project": patch
---

`tt-project`:

- a command schedule's `grace_min` (default 0: off) holds a keyed observation until its key has been reported that long, across runs and daemon restarts: a route that heals on its own raises no wake, note, issue, ask or claim on a short blip. Past the grace it raises one line at its own severity but at least `normal`, then repeats at `low` under the same key; held runs are never error runs. A normal run without the key, or a cleared line, ends its clock. `schedule_set` takes `grace_min` (0 removes it); `ttp schedules <project> grace <schedule> [key]` reads each key as ok, retrying or down. The worker and coordinator prompts say a failed try inside a route's grace is a retry, not a finding; tests
