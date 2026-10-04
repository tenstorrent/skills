---
"tt-project": patch
---

The `ttp` launcher now starts as `sh` and runs under the interpreter the daemon names in `TTP_PYTHON` (its own Python), falling back to `python3` from `PATH`. A broken or old `python3` in a project venv on a run's `PATH` no longer breaks `ttp note`, `ttp push` or `ttp lock` inside workers.
