---
"tt-project": patch
---

`tt-project`: a check that runs pytest behind a launcher after a `--` word (`launcher.py -- .venv/bin/python -m pytest -q tests`, `launcher.py -- pytest`) now counts as one pytest run. Its slowest tests are recorded and the push queue resumes it, as for a plain pytest check. Before, these checks silently lost both. The venv and nested-worktree changes are made at the pytest command itself, so the launcher keeps its own arguments. A check object takes a new `"pytest"` key: `true` marks a wrapped pytest run the command does not show, `false` turns detection off, and leaving it out detects it from the command. Text commands such as `grep -- pytest` or `echo -- pytest`, and paths that only contain `pytest`, are still not counted.
