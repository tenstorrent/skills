---
"tt-project": patch
---

`tt-project`: push checks (`ttp push` and `ttp push --own` without the push branch's priority slot) wait outside the host queue, keeping their arrival, while the host's load is above `checks.load_guard`, instead of holding a host-wide checks slot that other projects' checks could use. They take one once the load drops.
