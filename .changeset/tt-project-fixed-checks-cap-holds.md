---
"tt-project": patch
---

`tt-project`:

- a fixed `checks.test_project_max` now holds every run of the project, urgent and priority-1 ones too: a priority-1 run takes no over-cap lane past it, and an urgent run granted a reserved urgent slot still waits for room under the fixed cap before it runs. A sized (`auto`) cap keeps both shortcuts; landing checks keep their own lane.
- obligation owners in the checks queue are ordered by their earliest open due (`obligations.owner_due`): an owner due within 24 h, 4 h or 1 h (or overdue) ranks ahead of one due later, and all still rank ahead of every task priority.
- tests pin that push and landing checks count only running suites, so lanes held by runs still waiting for their host grants never block them.
