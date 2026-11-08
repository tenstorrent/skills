---
"tt-project": patch
---

A coordinator task_add whose spec context check fails now adds the task with a note naming the error,
instead of being rejected by an undefined name. The `DB` type used in the login-ask check is imported,
so pyflakes reports no undefined names in coordinator.py.
