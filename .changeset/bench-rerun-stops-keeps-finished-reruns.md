---
"tt-project": patch
---

Bench daily `--rerun-stops`: a run whose server waits were measured, or a rerun that ended, is never marked `server` again, and a stale mark of that kind is void wherever the last row of a cell is read (`serverhealth.latest`). A rerun keeps the earlier attempt's folder under the first free `.serverN` name, so a second rerun no longer fails on an existing folder, and a rerun already recorded is not graded again. The table's Server reruns lines lead with each rerun's outcome and say in words what stopped the first run.
