---
"tt-project": patch
---

`tt-project`: each `ttp bench run` cell runs its agent, and then its grade, in a fresh virtualenv of its own in the cell's temporary folder: its bin first on `PATH`, `VIRTUAL_ENV` set, `PYTHONNOUSERSITE=1` and `PIP_REQUIRE_VIRTUALENV=1`, `PIP_USER` unset and `PYTHONUSERBASE` in the cell. No cell can install into or import from the user site (`~/.local`) other projects on the host share, and nothing the agent installs runs the held-out tests. Each venv sees the OS's packages and gets a copy of the harness's installed pytest; a cell whose venv cannot be built is recorded `isolation_failed`.
