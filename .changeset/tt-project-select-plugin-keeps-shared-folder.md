---
"tt-project": patch
---

Targeted `ttp checks`: the select plugin keeps its folder on PYTHONPATH while PYTEST_ADDOPTS still loads another plugin from it (`-p name` or `-pname`), so in the added-tests rerun, which also loads ttp_pytest_rerun, a pytest a test starts with the inherited environment runs its own tests instead of failing to import ttp_pytest_rerun.
