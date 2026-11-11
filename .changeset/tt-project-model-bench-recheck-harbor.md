---
"tt-project": patch
---

`tt-project`:

- `ttp model-bench suite`: each model's result now feeds the chat model ranking. A parked model that passes its weekly recheck returns to the ranking; a weak result, or any followed injection, keeps it parked; an endpoint or isolation fault leaves the last verdict unchanged. `ttp model-bench show` lists each model's last verdict with its date and source, plus its last eight weekly rechecks.
- `ttp model-bench suite --harbor N` (off by default): an optional Terminal-Bench 2.0 slice through Harbor, installed only into a venv the run owns. A failed install skips the slice with its reason and tries no other route. Its row is for information only. Tested with fakes only.
