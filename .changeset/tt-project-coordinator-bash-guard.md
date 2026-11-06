---
"tt-project": patch
---

`tt-project`: a full-tools session coordinator's Bash calls are now checked before and after: a change to the charter, a memory file or `harness/project.json` that `ttp` itself did not write (a variable, glob or script write the command-text check misses) is put back and reported in the tool result and as a `coord_protected_write` event. `ttp`'s own writes to those files are logged in `state/protected-writes.jsonl`, so `ttp config`, memory actions and daemon writes during a call are kept. The state database is not compared; only the command-text check covers it. The guide now says a coordinator's device test run is denied with the `ttp sandbox` form as its hint, never rewritten.
