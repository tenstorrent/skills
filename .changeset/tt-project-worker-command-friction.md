---
"tt-project": patch
---

`ttp sandbox` and `ttp clip` drop pytest-xdist options (`-n`, `--numprocesses`, `--dist`, `--maxprocesses`) with a one-line notice where xdist is not installed. The worker prompt caps `pytest -n` only where xdist exists, and the code prompt and `ttp detach` output give the exact `ttp checks` forms (`--result <run dir>`, the `on_pass` JSON). A code worker's `waiting` hand-off on its own checks with no `on_pass`, `next_step`, PR or landing left now settles `done` on a pass without a model run.
