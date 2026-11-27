---
"tt-project": patch
---

The ask gate now refuses a `spend` or `funds` ask while the saved budget gates show no dollar cap reached or projected to be reached before its reset, and one about a task held on its own spent budget (the coordinator raises that with `task_update budget_usd`); asks naming a dollar cap or another spend setting only the user changes still go out. `ttp task requeue` refuses a task whose every obligation was dropped and names the route: `task_add` with `continues` and an open obligation key.
