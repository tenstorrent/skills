---
"tt-project": patch
---

Checks no longer inherit the harness runtime folder that tt-project puts on PYTHONPATH for its own `ttp` in worker runs: a project's tests import their own code, not the harness's, and an extra `PYTHONPATH=$PWD python3 -m pytest ...` after `ttp checks --` that matches a project check is dropped in worker runs too. Entries the user put on PYTHONPATH stay, in order.
